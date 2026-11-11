"""The speech-recognition worker: a child process OmniSeek starts to run the ASR models in.

Why a child (2026-11-10): SenseVoice on MPS adds about 2.4 GB to the process that loads it and up to
another 2.35 GB while a generate runs. Unloading gives the device memory back, but the malloc heap
MPSGraph builds per input shape stays with the process (560 / 1056 / 1305 MB after whole 10 / 30 / 87
minute files, 2480 MB after a segmented 87 minute run; the 10-11 memory readings). Inside OmniSeek's
long-lived process that residue could only be bounded by restarting OmniSeek. In a child it ends with
the child: the parent (omniseek.core.asr) starts one worker, sends it one job at a time, and ends it when
it has been idle, when its footprint leaves no room for another job's transient, or when a job has to
be stopped (over its memory cap, over its time, cancelled). OmniSeek's own process never imports torch
or funasr for speech.

Protocol: one JSON object per line on stdin (a job), one JSON object per line back on the channel the
parent reads (this process's original stdout, kept on its own descriptor). Everything else this
process writes, including funasr's print() banners, goes to stderr, which the parent passes through to
its log. A job is {"id", "op": "flat" | "segments" | "diar", "wav", "language", "speakers", "models"};
the answer is {"id", "ok": true, "result": ...} or {"id", "ok": false, "error": "..."}. The worker exits
on end of input, and on its own when its parent is gone (a parent killed mid-job cannot leave it running).

This module imports only the standard library at the top (torch and funasr load with the first job),
so starting it costs nothing the job does not need, and OmniSeek's process can import its pure helpers.
"""

from __future__ import annotations

import gc
import json
import os
import re
import sys
import threading
import time

MODEL = "iic/SenseVoiceSmall"
VAD = "fsmn-vad"
# The diarize=True path (WHO said what) loads a different ASR plus a speaker model; see Models.diar.
DIAR_MODEL = "paraformer-zh"
SPK_MODEL = "cam++"
PUNC_MODEL = "ct-punc"
SR = 16000
# funasr batch_size_s for the VAD-batched generate calls. Tried and dropped (10-11, readings in
# the 10-11 memory readings): the generate peak does NOT grow with audio length (whole-file peak footprint 5225 /
# 5164 / 4402 to 4466 MB for 10 / 30 / 87 min). Cutting long audio into 300 s windows RAISED the 87 min
# peak (5194 to 5474 MB), and so did BATCH_S = 60 (7578 MB): funasr's inference_with_vad sorts the VAD
# segments by length and, when segment j overflows batch_size_s, flushes a batch that still INCLUDES j,
# so a window or a small batch pads a long segment onto many short ones (600 s padded per batch instead
# of 311 s). PYTORCH_MPS_LOW_WATERMARK_RATIO=0.25 changed nothing (peak 5180 vs 5225 MB). Keep the
# whole-file path and 300.
BATCH_S = 300
# The diarization family's batch_size_s, measured apart (2026-11-10, a fresh process, a Chinese interview
# podcast, speakers=2): at 300 the generate peaked at 12421 MB for 10 min of audio (13605 for 2 min,
# 12452 for 5 min), i.e. Paraformer-large on MPS allocates for the batch, not for the audio; at 60, 30
# and 15 the same 10 min peaked at 6727, 6372 and 6237 MB with the identical result (3656 chars, 221
# turns, 2 speakers) in 62, 61 and 61 s instead of 85. 30 is where the gain flattens (15 saves 135 MB
# more for twice the generate calls). 30 min at 30 peaked at 6864 MB. Readings in docs/speech-worker.
DIAR_BATCH_S = 30

# SenseVoice emits rich tags <|zh|><|HAPPY|><|Speech|>… + emoji event markers; strip for clean text.
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF️]")
# A music/silence intro makes SenseVoice emit a stray leading sentence mark (「。」 etc.)
# with no speech before it; that mark is never legitimate at the very start. Strip a leading run of
# lone CJK/ASCII clause-or-sentence punctuation plus surrounding spaces.
_LEAD_PUNCT_RE = re.compile(r"^[\s。，、；：！？.,;:!?]+")

# How often the worker checks that its parent is still there (seconds). A parent that dies closes the
# job pipe, but the worker only reads it between jobs; this check ends a worker orphaned mid-job.
PARENT_POLL_S = 2.0


def clean(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return _LEAD_PUNCT_RE.sub("", text).strip()


def segments_from(spans: list, texts: list) -> list:
    """PURE assembler: zip VAD spans ``[[start_ms,end_ms],...]`` with the per-span transcribed texts into
    ``[{start,end,text}]`` (seconds, cleaned). Zips to the shorter length so a count mismatch degrades
    gracefully instead of misaligning; drops empty-text spans."""
    out = []
    for span, t in zip(spans or [], texts or []):
        if not (isinstance(span, (list, tuple)) and len(span) >= 2):
            continue
        txt = clean(t or "")
        if txt:
            out.append({"start": round(span[0] / 1000.0, 2),
                        "end": round(span[1] / 1000.0, 2), "text": txt})
    return out


def diarized_from(sentence_info: list) -> list:
    """PURE assembler: funasr ``sentence_info`` [{start,end,text/sentence,spk}] -> ``[{start,end,text,speaker}]``
    (seconds, cleaned, empty-text sentences dropped). ``speaker`` is the raw cluster index funasr assigned."""
    out = []
    for s in (sentence_info or []):
        if not isinstance(s, dict):
            continue
        txt = clean(s.get("text") or s.get("sentence") or "")
        st, en = s.get("start"), s.get("end")
        if not txt or st is None or en is None:
            continue
        out.append({"start": round(st / 1000.0, 2), "end": round(en / 1000.0, 2),
                    "text": txt, "speaker": s.get("spk")})
    return out


def _log(msg: str) -> None:
    sys.stderr.write(f"asr worker {os.getpid()}: {msg}\n")
    sys.stderr.flush()


def _auto_model():
    try:
        import funasr
    except ImportError as exc:
        from omniseek.core._optdep import install_hint  # loaded on this failure only
        raise RuntimeError("this feature needs the optional 'asr' dependencies (missing 'funasr'): "
                           f"{install_hint('asr')}") from exc
    return funasr.AutoModel


class Models:
    """The loaded models of this worker. One family at a time: loading the diarization model drops
    SenseVoice and its VAD, and loading either of those drops the diarization model. One job only ever
    uses one family, and the two together would not fit (SenseVoice+VAD measured +2.4 GB; the
    diarization stack, Paraformer 953 MB + ct-punc 1.1 GB + cam++ on disk, is larger)."""

    def __init__(self):
        self.sv = self.vad = self.diar = None
        self.ids: dict = {}

    def _drop(self, *names: str) -> None:
        had = [n for n in names if getattr(self, n) is not None]
        for n in names:
            setattr(self, n, None)
        if had:
            gc.collect()
            release_device()
            _log("dropped " + "+".join(had))

    def _load(self, name: str, **kw):
        AutoModel = _auto_model()
        last = None
        for dev in ("mps", "cpu"):
            try:
                t0 = time.monotonic()
                m = AutoModel(device=dev, disable_update=True, **kw)
                _log(f"{name} loaded on {dev} in {time.monotonic() - t0:.1f}s")
                return m
            except Exception as exc:  # noqa: BLE001
                last = exc
                _log(f"{name} load on {dev} failed: {exc}")
        raise RuntimeError(f"could not load {name} (mps/cpu): {last}")

    def get_sv(self, ids: dict):
        if self.sv is None or self.ids.get("sv") != (ids["model"], ids["vad"]):
            self._drop("diar", "sv")
            self.sv = self._load("SenseVoice", model=ids["model"], vad_model=ids["vad"],
                                 vad_kwargs={"max_single_segment_time": 30000})
            self.ids["sv"] = (ids["model"], ids["vad"])
        return self.sv

    def get_vad(self, ids: dict):
        if self.vad is None or self.ids.get("vad") != ids["vad"]:
            self._drop("diar", "vad")
            self.vad = self._load("fsmn-vad", model=ids["vad"])
            self.ids["vad"] = ids["vad"]
        return self.vad

    def get_diar(self, ids: dict):
        key = (ids["diar"], ids["vad"], ids["spk"], ids["punc"])
        if self.diar is None or self.ids.get("diar") != key:
            self._drop("sv", "vad", "diar")
            self.diar = self._load("diarization", model=ids["diar"], vad_model=ids["vad"],
                                   spk_model=ids["spk"], punc_model=ids["punc"])
            self.ids["diar"] = key
        return self.diar


def release_device() -> None:
    """Hand the MPS allocator's cached blocks back to the driver after every generate (torch 2.12 keeps
    every freed block until its low watermark, about 17 GB on a 16 GB machine, i.e. never). Never raises."""
    try:
        torch = sys.modules.get("torch")
        if torch is not None and torch.backends.mps.is_available():
            torch.mps.empty_cache()
    except Exception as exc:  # noqa: BLE001 -- a cache release is a memory bound, never a failure
        _log(f"mps empty_cache skipped ({exc})")


def _flat(models: Models, req: dict) -> str:
    try:
        res = models.get_sv(req["models"]).generate(
            input=req["wav"], cache={}, language=(req.get("language") or "auto"), use_itn=True,
            batch_size_s=BATCH_S, merge_vad=True, merge_length_s=15)
    finally:
        release_device()
    return clean(" ".join(r.get("text", "") for r in (res or [])))


def _segments(models: Models, req: dict) -> list:
    """Per-VAD-segment ``{start,end,text}``. SenseVoice emits NO per-segment offsets, so fsmn-vad runs
    STANDALONE for the [start_ms,end_ms] spans, the audio is sliced per span and the slices go through
    ONE batched generate. Returns [] on any hiccup (segments are an opt-in extra, never load-bearing)."""
    try:
        import soundfile as sf
        audio, sr = sf.read(req["wav"], dtype="float32")
        if getattr(audio, "ndim", 1) > 1:
            audio = audio[:, 0]
        try:
            vres = models.get_vad(req["models"]).generate(input=audio, fs=sr)
            spans = ((vres[0].get("value") if vres else None) or [])
            if not spans:
                return []
            slices = [audio[int(s * sr / 1000): int(e * sr / 1000)] for s, e in spans]
            res = models.get_sv(req["models"]).generate(
                input=slices, fs=sr, cache={}, language=(req.get("language") or "auto"),
                use_itn=True, merge_vad=False)
        finally:
            release_device()
        return segments_from(spans, [r.get("text", "") for r in (res or [])])
    except Exception as exc:  # noqa: BLE001
        _log(f"segments failed: {exc}")
        return []


def _diar(models: Models, req: dict) -> list:
    """[flat_text, segments, n_speakers] via the diarization model; ["", [], 0] on any hiccup (the caller
    degrades honestly, never a false transcript). speakers>0 pins cam++'s cluster count."""
    speakers = req.get("speakers")
    preset = speakers if (speakers and speakers > 0) else None
    try:
        try:
            res = models.get_diar(req["models"]).generate(
                input=req["wav"], cache={}, use_itn=True, batch_size_s=DIAR_BATCH_S, preset_spk_num=preset)
        finally:
            release_device()
        si = ((res[0] if isinstance(res, list) and res else {}) or {}).get("sentence_info") or []
        segs = diarized_from(si)
        flat = clean(" ".join(s["text"] for s in segs))
        n_spk = len({s["speaker"] for s in segs if s["speaker"] is not None})
        return [flat, segs, n_spk]
    except Exception as exc:  # noqa: BLE001
        _log(f"diarization failed: {exc}")
        return ["", [], 0]


OPS = {"flat": _flat, "segments": _segments, "diar": _diar}


def _watch_parent(parent: int) -> None:
    while True:
        time.sleep(PARENT_POLL_S)
        if os.getppid() != parent:
            _log("parent gone; exiting")
            os._exit(3)


def main() -> int:
    # The answer channel is the original stdout, moved to its own descriptor; stdout itself (fd 1, and
    # Python's sys.stdout) then points at stderr, so no library print can corrupt an answer line.
    chan = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    threading.Thread(target=_watch_parent, args=(os.getppid(),), name="asr-parent-watch",
                     daemon=True).start()
    models = Models()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        out = {"id": req.get("id")}
        try:
            out["result"] = OPS[req["op"]](models, req)
            out["ok"] = True
        except Exception as exc:  # noqa: BLE001 -- one bad job is an error answer, not a dead worker
            out["ok"] = False
            out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        chan.write(json.dumps(out, ensure_ascii=False) + "\n")
        chan.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
