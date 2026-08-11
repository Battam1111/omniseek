"""Local ASR — transcribe the SPOKEN content the agent cannot hear itself.

Model choice was BENCHMARKED on real code-switched Chinese podcast audio (not just CER tables):
SenseVoice-Small (FunAudioLLM / Alibaba, via funasr) produced an accurate, punctuated transcript
with correct Mandarin + code-switched English (CEO / AI / GX), at RTF ~0.03 (≈38× realtime) on the
M4 GPU (MPS). whisper-large-v3 on the SAME clip HALLUCINATED (the notorious "请点赞订阅转发打赏"
loop) — Whisper is unreliable on Chinese audio that opens with music/intros, i.e. most podcasts.
SenseVoice also leads the Mandarin CER benchmarks (2.96% vs Whisper-large-v3 5.14%) and ships
built-in punctuation + a VAD pipeline (fsmn-vad) that chunks long audio for free.

Flow: resolve an audio source (小宇宙 enclosure / bilibili via yt-dlp / direct file) → imageio-ffmpeg
decode to a 16k mono WAV (robust, no PATH/codec-backend reliance) → SenseVoice (funasr, MPS) with VAD
for long-form → strip the model's audio-event / emotion tags → cache FOREVER (spoken content never
changes). youtube does NOT route here (its adapter already returns captions). ASR is heavy to load +
slow-ish on long media, so it is an explicit, agent-driven tool (penumbra_transcribe), never a broad sweep.

Time-range transcription (2026-06-10): start/duration slice the audio BEFORE ASR (ffmpeg -ss/-t;
on direct/enclosure URLs -ss uses HTTP range seeking, so only the slice region is even downloaded).
The unit of transcription becomes "the segment judged worth hearing" (e.g. one chapter from a
podcast's shownotes timestamps) instead of a whole 3-hour episode nobody reads.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import random
import re
import subprocess
import tempfile
import time
from typing import Optional
from urllib.parse import urlencode, urlparse

import httpx

from penumbra.core import _netguard, _optdep, cache

logger = logging.getLogger(__name__)

_MODEL = "iic/SenseVoiceSmall"
_VAD = "fsmn-vad"
_SR = 16000
_MAX_SECONDS = 4 * 3600          # safety cap (4h of audio)
_FFMPEG_RW_TIMEOUT_US = 30_000_000    # 30s: abort a STALLED network read (no infinite ffmpeg hang)
_MIN_WAV_BYTES = 2000                 # a decode yielding < this (~header only) = a failed remote fetch
_AUDIO_DL_MAX_BYTES = 500 * 1024 * 1024   # cap for the robust-download fallback (podcasts run large)
_TTL = 365 * 24 * 3600           # transcripts never change → cache ~forever
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0 Safari/537.36"
# bilibili's gaia WAF cross-checks the UA against the fingerprint it activates, so the bilibili
# path uses ONE consistent real-Chrome UA everywhere (headers + the ExClimbWuzhi payload).
_BILI_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_AUDIO_EXT = (".mp3", ".m4a", ".wav", ".aac", ".ogg", ".flac", ".opus")
# SenseVoice emits rich tags <|zh|><|HAPPY|><|Speech|>… + emoji event markers; strip for clean text.
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿\U0001F1E6-\U0001F1FF️]")
# A music/silence intro makes SenseVoice emit a stray leading sentence mark (「。」 etc.)
# with no speech before it; that mark is never legitimate at the very start. Strip a leading run of
# lone CJK/ASCII clause-or-sentence punctuation plus surrounding spaces.
_LEAD_PUNCT_RE = re.compile(r"^[\s。，、；：！？.,;:!?]+")

_model = None  # lazy global singleton (load is expensive; keep warm across calls)


def _get_model():
    global _model
    if _model is None:
        AutoModel = _optdep.require("funasr", "asr").AutoModel
        last = None
        for dev in ("mps", "cpu"):
            try:
                _model = AutoModel(model=_MODEL, vad_model=_VAD,
                                   vad_kwargs={"max_single_segment_time": 30000},
                                   device=dev, disable_update=True)
                logger.info("SenseVoice loaded on %s", dev)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
                logger.warning("SenseVoice load on %s failed: %s", dev, exc)
        if _model is None:
            raise RuntimeError(f"could not load SenseVoice (mps/cpu): {last}")
    return _model


def _ffmpeg_exe() -> str:
    imageio_ffmpeg = _optdep.require("imageio_ffmpeg", "asr")
    return imageio_ffmpeg.get_ffmpeg_exe()


def _parse_ts(v) -> Optional[float]:
    """Timestamp → seconds. Accepts seconds ("750" / 750 / 90.5) or clock form
    ("12:30" / "1:02:30" — the shapes podcast shownotes use). None/"" → None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return max(float(v), 0.0)
    s = str(v).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) > 3 or not all(p.strip().replace(".", "", 1).isdigit() for p in parts):
        raise ValueError(f"bad timestamp {v!r} — use seconds ('750') or MM:SS / HH:MM:SS ('12:30', '1:02:30')")
    secs = 0.0
    for p in parts:
        secs = secs * 60 + float(p)
    return secs


def _decode_to_wav(src: str, start_s: Optional[float] = None, dur_s: Optional[float] = None) -> str:
    """ffmpeg-decode any audio file/URL to a temp 16k mono WAV; return its path (caller removes).

    start_s/dur_s slice the audio: -ss before -i is a fast input seek (HTTP range
    on remote URLs, so a 10-min slice of a 3-hour episode never downloads the rest)."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="penumbra-asr-")
    os.close(fd)
    # Lock ffmpeg's -i protocol surface per source kind: by default ffmpeg honors file:// (local-file
    # read) and http(s):// to ANY host (SSRF) on its input. A remote URL is SSRF-guarded + denied the
    # file protocol; a plain local path (a temp file WE created) gets only the file protocol; anything
    # with a scheme that is not http(s) (e.g. file://, concat:, subfile:) is refused outright.
    scheme = urlparse(src).scheme.lower()
    if scheme in ("http", "https"):
        _blk = _netguard.security_block_reason(src)
        if _blk is not None:
            try:
                os.remove(path)
            except OSError:
                pass
            raise RuntimeError(f"refused remote audio url ({_blk}): {src[:120]}")
        _proto = "https,http,tcp,tls,crypto"
    elif "://" not in src:
        _proto = "file"
    else:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(f"refused ffmpeg input scheme: {scheme or '?'}")
    cmd = [_ffmpeg_exe(), "-nostdin", "-hide_banner", "-loglevel", "error",
           "-rw_timeout", str(_FFMPEG_RW_TIMEOUT_US),  # bound a STALLED network read: no infinite hang
           "-protocol_whitelist", _proto]
    if start_s:
        cmd += ["-ss", str(start_s)]
    cmd += ["-i", src, "-t", str(min(dur_s, _MAX_SECONDS) if dur_s else _MAX_SECONDS),
            "-ac", "1", "-ar", str(_SR), "-y", path]
    proc = subprocess.run(cmd, capture_output=True)
    # The bundled ffmpeg's TLS (macOS SecureTransport) intermittently fails to negotiate with some CDNs
    # (-9806 on cloudfront / anchor), surfacing two ways: a non-zero exit, OR exit 0 with an empty /
    # near-empty WAV. On a REMOTE url either way, fall back to a robust httpx (openssl) download of the
    # whole file, then decode it LOCALLY (file protocol, ffmpeg never touches TLS). The fast ffmpeg
    # range-seek stays the default for CDNs it handles fine (e.g. xyzcdn), so slice efficiency is kept.
    if scheme in ("http", "https") and (proc.returncode != 0 or os.path.getsize(path) < _MIN_WAV_BYTES):
        _rm(path)
        dl = None
        try:
            fd2, dl = tempfile.mkstemp(suffix=".audio", prefix="penumbra-asr-dl-")
            os.close(fd2)
            from penumbra.core import http as _http  # lazy: asr stays import-light
            _http.download_to_file(src, dl, max_bytes=_AUDIO_DL_MAX_BYTES)
            return _decode_to_wav(dl, start_s, dur_s)  # local path -> "file" proto, no ffmpeg TLS
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"ffmpeg direct decode failed and robust-download fallback failed: {exc}") from exc
        finally:
            _rm(dl)
    if proc.returncode != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg decode failed: {proc.stderr.decode('utf-8', 'ignore')[:200]}")
    return path


def _clean(text: str) -> str:
    text = _TAG_RE.sub("", text or "")
    text = _EMOJI_RE.sub("", text)
    text = re.sub(r"[ \t]{2,}", " ", text).strip()
    return _LEAD_PUNCT_RE.sub("", text).strip()


def _transcribe_wav(wav_path: str, language: Optional[str]) -> str:
    m = _get_model()
    res = m.generate(input=wav_path, cache={}, language=(language or "auto"),
                     use_itn=True, batch_size_s=300, merge_vad=True, merge_length_s=15)
    return _clean(" ".join(r.get("text", "") for r in (res or [])))


# ── segment timestamps (opt-in): SenseVoice emits NO per-segment offsets and the combined model's
# merge_vad=False surfaces text but not timestamps (verified on the mini, funasr 1.3.9). So we run
# fsmn-vad STANDALONE for the [start_ms,end_ms] spans, slice the audio per span, and batch-transcribe
# the slices in ONE generate call. This is the scaffold a --diarize turn hangs speaker labels on, and
# on its own makes a no-shownote transcript navigable / time-citable. The default flat transcript path
# (above) is untouched, so segments carries zero cost / zero regression when not requested. ────────
_vad_model = None  # lazy standalone fsmn-vad singleton (segment offsets)


def _get_vad_model():
    global _vad_model
    if _vad_model is None:
        AutoModel = _optdep.require("funasr", "asr").AutoModel
        last = None
        for dev in ("mps", "cpu"):
            try:
                _vad_model = AutoModel(model=_VAD, device=dev, disable_update=True)
                logger.info("fsmn-vad (segments) loaded on %s", dev)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if _vad_model is None:
            raise RuntimeError(f"could not load fsmn-vad (mps/cpu): {last}")
    return _vad_model


def _segments_from(spans: list, texts: list) -> list[dict]:
    """PURE assembler (the smoke-golden target): zip VAD spans ``[[start_ms,end_ms],...]`` with the
    per-span transcribed texts into ``[{start,end,text}]`` (seconds, cleaned). Zips to the shorter
    length so a count mismatch degrades gracefully instead of misaligning; drops empty-text spans."""
    out = []
    for span, t in zip(spans or [], texts or []):
        if not (isinstance(span, (list, tuple)) and len(span) >= 2):
            continue
        txt = _clean(t or "")
        if txt:
            out.append({"start": round(span[0] / 1000.0, 2),
                        "end": round(span[1] / 1000.0, 2), "text": txt})
    return out


def _transcribe_segments(wav_path: str, language: Optional[str]) -> list[dict]:
    """Per-VAD-segment ``{start,end,text}`` for a wav. fsmn-vad standalone for the offsets -> slice per
    span -> ONE batched generate for the texts -> zip. Returns [] on any hiccup (the caller keeps the
    flat transcript regardless; segments are opt-in extra, never load-bearing)."""
    try:
        import soundfile as sf
        audio, sr = sf.read(wav_path, dtype="float32")
        if getattr(audio, "ndim", 1) > 1:
            audio = audio[:, 0]  # mono (decode is already mono; belt-and-suspenders)
        vres = _get_vad_model().generate(input=audio, fs=sr)
        spans = ((vres[0].get("value") if vres else None) or [])
        if not spans:
            return []
        slices = [audio[int(s * sr / 1000): int(e * sr / 1000)] for s, e in spans]
        res = _get_model().generate(input=slices, fs=sr, cache={}, language=(language or "auto"),
                                    use_itn=True, merge_vad=False)
        return _segments_from(spans, [r.get("text", "") for r in (res or [])])
    except Exception as exc:  # noqa: BLE001 — segments are opt-in; never break the transcript
        logger.warning("asr segments failed %s: %s", wav_path, exc)
        return []


# ── speaker diarization (opt-in --diarize): WHO-said-what. funasr's timestamp-based speaker attribution
# needs a TIMESTAMPED ASR, and SenseVoice emits none (it crashes distribute_spk), so the diarize path
# uses Paraformer-zh + fsmn-vad + cam++ (speaker embed + cluster) + ct-punc (sentence boundaries), ALL
# from modelscope (NO HuggingFace token, NO gated terms), lazy-loaded ONLY when --diarize is requested
# (zero steady-state footprint on the mini). funasr does VAD + embed + cluster + attribution internally;
# we read sentence_info. zh-focused (Paraformer-zh); the flat transcript path keeps SenseVoice. ───────
_diar_model = None


def _get_diar_model():
    global _diar_model
    if _diar_model is None:
        AutoModel = _optdep.require("funasr", "asr").AutoModel
        last = None
        for dev in ("mps", "cpu"):
            try:
                _diar_model = AutoModel(model="paraformer-zh", vad_model=_VAD, spk_model="cam++",
                                        punc_model="ct-punc", device=dev, disable_update=True)
                logger.info("diarization model (paraformer-zh+cam++) loaded on %s", dev)
                break
            except Exception as exc:  # noqa: BLE001
                last = exc
        if _diar_model is None:
            raise RuntimeError(f"could not load diarization model (mps/cpu): {last}")
    return _diar_model


def _diarized_from(sentence_info: list) -> list[dict]:
    """PURE assembler (the smoke-golden target): funasr ``sentence_info`` [{start,end,text/sentence,spk}]
    -> ``[{start,end,text,speaker}]`` (seconds, cleaned, empty-text sentences dropped). ``speaker`` is the
    raw cluster index funasr assigned (the agent reads 'speaker 0 said X, speaker 1 said Y')."""
    out = []
    for s in (sentence_info or []):
        if not isinstance(s, dict):
            continue
        txt = _clean(s.get("text") or s.get("sentence") or "")
        st, en = s.get("start"), s.get("end")
        if not txt or st is None or en is None:
            continue
        out.append({"start": round(st / 1000.0, 2), "end": round(en / 1000.0, 2),
                    "text": txt, "speaker": s.get("spk")})
    return out


def _transcribe_diarized(wav_path: str, speakers: Optional[int] = None) -> tuple:
    """(flat_text, segments, n_speakers) via the diarization model. funasr does the whole pipeline; we
    read sentence_info -> per-sentence {start,end,text,speaker}. ("", [], 0) on any hiccup (the caller
    keeps degrading honestly, never a false transcript).

    speakers>0 pins cam++'s cluster count to that oracle number (preset_spk_num); the eigengap
    auto-estimate is unstable on short / noisy audio (it over- or under-splits), so passing the known
    count (a 1-on-1 = 2, a solo = 1) is what makes the labels track the real turns."""
    preset = speakers if (speakers and speakers > 0) else None
    try:
        res = _get_diar_model().generate(input=wav_path, cache={}, use_itn=True, batch_size_s=300,
                                         preset_spk_num=preset)
        si = ((res[0] if isinstance(res, list) and res else {}) or {}).get("sentence_info") or []
        segs = _diarized_from(si)
        flat = _clean(" ".join(s["text"] for s in segs))
        n_spk = len({s["speaker"] for s in segs if s["speaker"] is not None})
        return flat, segs, n_spk
    except Exception as exc:  # noqa: BLE001 — diarize is opt-in; never break the caller
        logger.warning("asr diarization failed %s: %s", wav_path, exc)
        return "", [], 0


def _xiaoyuzhou_audio(url: str) -> tuple[Optional[str], dict]:
    """Resolve a 小宇宙 episode page URL → direct audio enclosure URL (+ meta)."""
    try:
        r = httpx.get(url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
        pp = json.loads(_NEXT.search(r.text).group(1))["props"]["pageProps"]
        ep = pp.get("episode") or {}
        enc = ep.get("enclosure") or {}
        au = enc.get("url") or ((ep.get("media") or {}).get("source") or {}).get("url")
        return au, {"source": "xiaoyuzhou", "title": ep.get("title"),
                    "podcast": (ep.get("podcast") or {}).get("title")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("xiaoyuzhou audio resolve failed %s: %s", url, exc)
        return None, {}


# ── bilibili: own-session activated playurl (yt-dlp structurally can't do this) ──────────────
# bilibili's gaia WAF returns HTTP 412 on x/player/wbi/playurl until the buvid session is
# ACTIVATED via the ExClimbWuzhi gaia-gateway call. yt-dlp never performs that activation — and
# feeding it the activated cookies STILL 412s, because it re-fetches webpage/nav and loses the
# activation binding. So bilibili bypasses yt-dlp entirely: we drive the full browser-style
# bootstrap in ONE cookie session, then download the DASH audio CDN directly. Proven end-to-end
# from our US-egress IP, no login + no China IP needed (2026-06-17): the 412 is risk-control /
# session-trust, NOT an IP block (popular/view/nav all return 200 from the same IP; only the
# un-activated playurl is gated). The two magic constants below are bilibili-controlled and DO
# rotate — if this 412s again, re-verify THESE first (the activation STRUCTURE is the durable part):
#   _BILI_TICKET_KEY  — the HMAC-SHA256 key for the GenWebTicket web-ticket signature
#   _BILI_WBI_TAB     — the 64-entry mixin-key reorder table for WBI request signing
_BILI_TICKET_KEY = "XgwSnGZ1p"
_BILI_WBI_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
                 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
                 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
                 20, 34, 44, 52]
_BVID_RE = re.compile(r"BV[0-9A-Za-z]{10}")


def _bili_wbi_signed_query(params: dict, wbi_key: str) -> str:
    """WBI-sign params → the EXACT query string to send. Sort by key, drop the chars bilibili
    strips, then w_rid = md5(querystring + mixinKey). The signed query MUST be sent verbatim
    (re-encoding it elsewhere would change w_rid and fail the signature)."""
    params = dict(params, wts=int(time.time()))
    clean = {k: "".join(ch for ch in str(v) if ch not in "!'()*")
             for k, v in sorted(params.items())}
    q = urlencode(clean)
    return q + "&w_rid=" + hashlib.md5((q + wbi_key).encode()).hexdigest()


def bilibili_playurl(url: str) -> Optional[dict]:
    """Activated-session playurl for a bilibili video -> its DASH streams + the handles to fetch
    them. Runs the full browser-style bootstrap (homepage -> spi buvid3/4 -> synth _uuid/b_lsid ->
    bili_ticket -> ExClimbWuzhi activation -> WBI-signed playurl) in ONE httpx session. Shared by
    _bilibili_audio (audio) and vframes (video). The 412 is risk-control, not IP/login (see the
    block comment above). Returns None on any failure.

    {"bvid", "cid", "title", "author", "duration", "dash": {"audio": [...], "video": [...]},
     "cookies": {name: val}, "referer": str}  -- CDN baseUrls are pre-signed; fetch them with the
    referer (+ cookies) via _bili_download."""
    m = _BVID_RE.search(url)
    bvid = m.group(0) if m else None
    base_headers = {"User-Agent": _BILI_UA, "Referer": "https://www.bilibili.com/",
                    "Origin": "https://www.bilibili.com"}
    rh = lambda n: "".join(random.choice("0123456789ABCDEF") for _ in range(n))  # noqa: E731

    def ck(c, name, val):
        c.cookies.set(name, val, domain=".bilibili.com")

    try:
        with httpx.Client(headers=base_headers, follow_redirects=True, timeout=30) as c:
            # b23.tv (or any non-BV URL) -> follow to the real video page for its BVID
            if bvid is None:
                r = c.get(url)
                m = _BVID_RE.search(str(r.url)) or _BVID_RE.search(r.text)
                if not m:
                    logger.warning("bilibili: no BVID resolvable from %s", url)
                    return None
                bvid = m.group(0)

            c.get("https://www.bilibili.com/")  # seeds buvid3 / b_nut
            spi = c.get("https://api.bilibili.com/x/frontend/finger/spi").json()
            ck(c, "buvid3", spi["data"]["b_3"])
            ck(c, "buvid4", spi["data"]["b_4"])

            ts = int(time.time() * 1000)
            ck(c, "_uuid", "%s-%s-%s-%s-%s%dinfoc" % (rh(8), rh(4), rh(4), rh(4), rh(5),
                                                      int(str(ts)[-5:])))
            ck(c, "b_lsid", "%s_%X" % (rh(8), ts))

            # bili_ticket (web-ticket HMAC); harmless to proceed if it fails, but it helps trust
            t = int(time.time())
            hexsign = hmac.new(_BILI_TICKET_KEY.encode(), ("ts%d" % t).encode(),
                               hashlib.sha256).hexdigest()
            tj = c.post("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
                        "?key_id=ec02&hexsign=%s&context[ts]=%d&csrf=" % (hexsign, t)).json()
            if tj.get("code") == 0:
                ck(c, "bili_ticket", tj["data"]["ticket"])
                ck(c, "bili_ticket_expires", str(t + tj["data"]["ttl"]))

            # ExClimbWuzhi gaia activation -- THE missing piece that flips playurl 412 -> 200.
            payload = {"3064": 1, "5062": str(ts), "03bf": "https://www.bilibili.com/",
                       "39c8": "333.1007.fp.risk", "6e7c": "878x1080",
                       "3c43": {"b8ce": _BILI_UA, "07a4": "zh-CN", "6aa9": "Asia/Shanghai"}}
            c.post("https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi",
                   json={"payload": json.dumps(payload)})

            vj = c.get("https://api.bilibili.com/x/web-interface/view",
                       params={"bvid": bvid}).json()
            data = vj.get("data") or {}
            cid = data.get("cid")
            if not cid:
                logger.warning("bilibili: no cid for %s (code %s)", bvid, vj.get("code"))
                return None

            nav = c.get("https://api.bilibili.com/x/web-interface/nav").json()
            wi = nav["data"]["wbi_img"]
            orig = (wi["img_url"].rsplit("/", 1)[-1].split(".")[0]
                    + wi["sub_url"].rsplit("/", 1)[-1].split(".")[0])
            wbi_key = "".join(orig[i] for i in _BILI_WBI_TAB)[:32]

            query = _bili_wbi_signed_query({"bvid": bvid, "cid": cid, "fnval": 4048,
                                            "try_look": 1}, wbi_key)
            ref = "https://www.bilibili.com/video/%s" % bvid
            r = c.get("https://api.bilibili.com/x/player/wbi/playurl?" + query,
                      headers={"Referer": ref})
            if r.status_code != 200:
                logger.warning("bilibili playurl HTTP %s for %s (activation may have failed)",
                               r.status_code, bvid)
                return None
            dash = (r.json().get("data") or {}).get("dash") or {}
            return {
                "bvid": bvid, "cid": cid,
                "title": data.get("title"),
                "author": (data.get("owner") or {}).get("name"),
                "duration": data.get("duration"),
                "dash": {"audio": dash.get("audio") or [], "video": dash.get("video") or []},
                "cookies": dict(c.cookies), "referer": ref,
            }
    except Exception as exc:  # noqa: BLE001
        logger.warning("bilibili playurl failed %s: %s", url, exc)
        return None


def _bili_stream_urls(stream: dict) -> list:
    """A DASH stream's CDN urls (baseUrl + backups), in fetch order."""
    return ([u for u in (stream.get("baseUrl"), stream.get("base_url")) if u]
            + list(stream.get("backupUrl") or stream.get("backup_url") or []))


def _bili_download(urls: list, cookies: dict, referer: str, dest: str) -> bool:
    """Download the first working bilibili CDN url to ``dest`` (a DASH .m4s; pre-signed, but the
    CDN requires the video-page Referer + the activated cookies). True on success."""
    hdrs = {"User-Agent": _BILI_UA, "Referer": referer}
    for u in urls:
        try:
            with httpx.stream("GET", u, headers=hdrs, cookies=cookies, timeout=120,
                              follow_redirects=True) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes(262144):
                        f.write(chunk)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("bilibili CDN url failed (%s): %s", str(u)[:60], exc)
    return False


def _bilibili_audio(url: str) -> tuple[Optional[str], dict]:
    """Resolve a bilibili video URL -> its smallest DASH AUDIO stream, downloaded to a temp .m4s.
    Returns (path, meta); caller removes the file + its dir. (None, {}) on failure."""
    pl = bilibili_playurl(url)
    if not pl:
        return None, {}
    audio = pl["dash"]["audio"]
    if not audio:
        logger.warning("bilibili: no audio stream for %s", pl.get("bvid"))
        return None, {}
    lo = min(audio, key=lambda a: a.get("bandwidth", 0))
    tmpdir = tempfile.mkdtemp(prefix="penumbra-asr-dl-")
    path = os.path.join(tmpdir, "a.m4s")
    if _bili_download(_bili_stream_urls(lo), pl["cookies"], pl["referer"], path):
        return path, {"source": "bilibili", "title": pl.get("title"), "author": pl.get("author")}
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, {}


def _ytdlp_download(url: str) -> tuple[Optional[str], dict]:
    """Download bestaudio to a temp file via yt-dlp (non-bilibili video/podcast hosts — bilibili
    has its own activated-session path that yt-dlp structurally can't do; see _bilibili_audio).
    A real UA is the difference between working and blocked. Returns (path, meta); caller removes
    the file + its dir."""
    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="penumbra-asr-dl-")
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio/best", "outtmpl": os.path.join(tmpdir, "a.%(ext)s"),
            "postprocessors": [], "http_headers": {"User-Agent": _UA}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if not f.endswith(".txt")]
        path = files[0] if files else None
        return path, {"source": (info.get("extractor") or "video").split(":")[0],
                      "title": info.get("title"), "author": info.get("uploader")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("yt-dlp audio download failed %s: %s", url, exc)
        return None, {}


def _rm(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
    d = os.path.dirname(path)
    if os.path.basename(d).startswith("penumbra-asr-dl-"):
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ── douyin: capture play_addr via the logged-in 9225 Chrome (yt-dlp's Douyin extractor is broken) ──
# yt-dlp 2026.06.09's Douyin extractor rejects even real logged-in cookies ("Fresh cookies (not
# necessarily logged in) are needed", tested 2026-06-23 both via cookiesfrombrowser and a 59-cookie
# CDP-exported jar), so we bypass it the same way _bilibili_audio bypasses yt-dlp for bilibili: drive
# the 9225 douyin Chrome to the video page, CAPTURE its own detail XHR (/aweme/v1/web/aweme/detail/ —
# the page signs a-bogus INTERNALLY, we never sign anything), read video.play_addr.url_list, and
# download the pre-signed CDN mp4 (UA + Referer, NO cookies needed — tested HTTP 200 video/mp4). The
# audio rides inside that mp4; the shared _decode_to_wav extracts it for SenseVoice.
_DOUYIN_CDP = "http://127.0.0.1:9225"


def _douyin_audio(url: str) -> tuple[Optional[str], dict]:
    """Resolve a douyin video's media file by capturing its play_addr off the page's own (internally
    a-bogus-signed) detail XHR via the 9225 Chrome, then downloading the pre-signed CDN mp4. Returns
    (path, meta) or (None, {}); the caller removes the file + its dir."""
    from penumbra.core.sources.walled._cdp import cdp_call  # lazy: avoid import-time CDP dependency

    def _flow(page):
        out = {"play_url": None, "title": None, "author": None}

        def _on_resp(r):
            try:
                if "/aweme/v1/web/aweme/detail" in r.url and not out["play_url"]:
                    d = r.json().get("aweme_detail") or {}
                    urls = (((d.get("video") or {}).get("play_addr") or {}).get("url_list")) or []
                    if urls:
                        out["play_url"] = urls[0]
                        out["title"] = d.get("desc")
                        out["author"] = (d.get("author") or {}).get("nickname")
            except Exception:  # noqa: BLE001
                pass

        page.on("response", _on_resp)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        for _ in range(12):  # poll up to ~12s for the detail XHR to fire + carry play_addr
            page.wait_for_timeout(1000)
            if out["play_url"]:
                break
        return out

    try:
        r = cdp_call(_flow, initial_url=None, cdp_url=_DOUYIN_CDP, timeout=75)
    except Exception as exc:  # noqa: BLE001 — CDP/flow failure → degrade (the contract)
        logger.warning("douyin audio: CDP flow failed %s: %s", url, exc)
        return None, {}
    play_url = (r or {}).get("play_url")
    if not play_url:
        logger.warning("douyin audio: no play_addr captured (9225 logged out / 风控?) for %s", url)
        return None, {}
    tmpdir = tempfile.mkdtemp(prefix="penumbra-asr-dl-")
    dest = os.path.join(tmpdir, "a.mp4")
    try:
        with httpx.stream("GET", play_url, follow_redirects=True, timeout=120,
                          headers={"User-Agent": _UA, "Referer": "https://www.douyin.com/"}) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    except Exception as exc:  # noqa: BLE001
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.warning("douyin audio: download failed %s: %s", play_url[:80], exc)
        return None, {}
    return dest, {"source": "Douyin", "title": r.get("title"), "author": r.get("author")}


def _remember_transcript(rec: dict) -> None:
    """Perception-produced text enters the eye's MEMORY (2026-07-05): a transcript is text the eye
    itself produced, so it must be findable later ("which podcast said X") exactly like fetched
    text — before this, a transcript lived only in the URL-keyed cache, unreachable by content.
    Full-lane ingest via recall.writer.ingest_produced (same single-writer queue; WRITES_ENABLED /
    fail-open gates keep cron processes writing nothing). Fires on BOTH fresh and cached returns:
    the upsert is idempotent on (source, source_id), and a repeat call heals pre-fix transcripts
    into the index (re-segmenting them on a SEG_VERSION bump). Slices are keyed AND titled
    per-slice so two slices of one episode stay distinct docs (same bare title would title-
    fingerprint-merge at read time). Best-effort: memory must never break perception."""
    try:
        text = (rec.get("transcript") or "").strip()
        url = rec.get("url") or ""
        if not text or not url or rec.get("error"):
            return
        s0, dur = rec.get("start_seconds"), rec.get("duration_seconds")
        if s0 or dur:
            slice_tag = f"{int(s0 or 0)}s+{int(dur)}s" if dur else f"{int(s0 or 0)}s+"
            sid = f"{url}#t={slice_tag}"
            title = f"{rec.get('title') or url} [transcript {slice_tag}]"
        else:
            sid = url
            title = f"{rec.get('title') or url} [transcript]"
        from penumbra.core.normalize import Document
        from penumbra.core.recall import writer
        writer.ingest_produced([Document(
            source="asr", source_id=sid, url=url, title=title, content=text,
            tags=["transcript"],
            metadata={"asr_model": rec.get("model"), "audio_seconds": rec.get("audio_seconds"),
                      "media_source": rec.get("source")},
        )])
    except Exception as exc:  # noqa: BLE001 — memory must never break perception
        logger.debug("transcript memory ingest skipped: %s", exc)


def transcribe_url(url: str, language: Optional[str] = None,
                   start=None, duration=None, segments: bool = False, diarize: bool = False,
                   speakers: Optional[int] = None) -> dict:
    """Resolve audio for `url` → SenseVoice transcript. Cached forever. See penumbra_transcribe for fields.

    start/duration (seconds or MM:SS / HH:MM:SS) transcribe only that slice — the
    chapter-from-shownotes pattern for long episodes. segments=True ALSO returns a per-VAD-segment
    [{start,end,text}] list (seconds) so a no-shownote transcript is navigable / time-citable; the flat
    ``transcript`` is unchanged either way.

    diarize=True switches to the WHO-said-what path (Paraformer-zh + cam++, zh-focused): ``segments``
    become [{start,end,text,speaker}] and ``speakers`` carries the distinct-speaker count. It is a
    SEPARATE ASR pass (Paraformer, not SenseVoice) so the flat ``transcript`` is the diarized text
    joined; use it for interviews / multi-host podcasts where speaker turns matter. Pass ``speakers=N``
    (the known head-count) to pin cam++'s cluster count; leaving it None auto-estimates (unstable on
    short audio)."""
    url = (url or "").strip()
    if not url:
        return {"url": url, "error": "empty url", "transcript": ""}
    try:
        start_s, dur_s = _parse_ts(start), _parse_ts(duration)
    except ValueError as exc:
        return {"url": url, "error": str(exc), "transcript": ""}
    ck = cache.make_key("asr", _MODEL, url, language or "auto",
                        f"{start_s or 0}-{dur_s or 'full'}", f"seg{int(bool(segments))}",
                        f"diar{int(bool(diarize))}", f"spk{int(speakers or 0)}")
    cached = cache.get(ck)
    if cached is not None:
        rec = {**cached, "cached": True}
        _remember_transcript(rec)  # heal: pre-fix transcripts enter the memory too (idempotent)
        return rec

    scheme = (urlparse(url).scheme or "").lower()
    host = (urlparse(url).hostname or "").lower()
    # SSRF guard: transcribe_url takes an AGENT-controlled URL and the yt-dlp else-branch below
    # hands it straight to yt-dlp (a SEPARATE egress from the ffmpeg _decode_to_wav guard). Refuse
    # an SSRF-class http(s) URL here so no branch can reach a loopback/private host (127.0.0.1:9222
    # CDP). Non-http schemes are already rejected by _decode_to_wav's own guard.
    if scheme in ("http", "https"):
        _blk = _netguard.security_block_reason(url)
        if _blk:
            return {"url": url, "error": f"refused: {_blk}", "transcript": ""}

    def _host_is(domain: str) -> bool:  # SUFFIX match, not substring: xiaoyuzhoufm.com.evil.net must NOT route
        return host == domain or host.endswith("." + domain)

    t0 = time.time()
    ytmp = wav = None
    try:
        if _host_is("xiaoyuzhoufm.com"):
            au, meta = _xiaoyuzhou_audio(url)
            if not au:
                return {"url": url, "error": "could not resolve 小宇宙 audio", "transcript": ""}
            wav = _decode_to_wav(au, start_s, dur_s)
        elif _host_is("bilibili.com") or _host_is("b23.tv"):
            ytmp, meta = _bilibili_audio(url)  # own activated session — yt-dlp 412s on bilibili
            if not ytmp:
                return {"url": url, "error": "could not resolve bilibili audio (anti-crawler 412?)", "transcript": ""}
            wav = _decode_to_wav(ytmp, start_s, dur_s)
        elif _host_is("douyin.com"):
            ytmp, meta = _douyin_audio(url)  # yt-dlp's Douyin extractor is broken; capture play_addr via 9225
            if not ytmp:
                return {"url": url, "error": "could not resolve douyin audio (9225 session down / play_addr miss)", "transcript": ""}
            wav = _decode_to_wav(ytmp, start_s, dur_s)
        elif url.lower().split("?")[0].endswith(_AUDIO_EXT):
            meta = {"source": "audio"}
            wav = _decode_to_wav(url, start_s, dur_s)
        else:  # other video/podcast hosts → yt-dlp download (robust)
            ytmp, meta = _ytdlp_download(url)
            if not ytmp:
                return {"url": url, "error": "no audio stream resolved (unsupported host?)", "transcript": ""}
            wav = _decode_to_wav(ytmp, start_s, dur_s)
        audio_secs = round(os.path.getsize(wav) / (_SR * 2))  # 16-bit mono
        diar_segs, n_spk, seg = [], 0, None
        if diarize:  # WHO-said-what: the Paraformer diarization model gives BOTH the flat text + speaker segments
            text, diar_segs, n_spk = _transcribe_diarized(wav, speakers)
        else:
            text = _transcribe_wav(wav, language)
            seg = _transcribe_segments(wav, language) if segments else None  # compute while the wav still exists
    except Exception as exc:  # noqa: BLE001
        logger.warning("asr failed %s: %s", url, exc)
        return {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "transcript": ""}
    finally:
        _rm(wav)
        _rm(ytmp)

    rec = {"url": url, "transcript": text, "chars": len(text),
           "audio_seconds": audio_secs, "asr_seconds": round(time.time() - t0, 1),
           "model": "paraformer-zh+cam++" if diarize else "SenseVoice-Small", "source": meta.get("source"),
           "title": meta.get("title"), "cached": False}
    if diarize:
        rec["segments"] = diar_segs  # [{start,end,text,speaker}]; WHO-said-what turns
        rec["speakers"] = n_spk      # distinct-speaker count funasr's cam++ clustered
    elif segments:
        rec["segments"] = seg or []  # per-VAD-segment [{start,end,text}]; opt-in navigable view
    if start_s or dur_s:
        rec["start_seconds"] = start_s or 0
        rec["duration_seconds"] = dur_s
    cache.set(ck, rec, ttl=_TTL)
    _remember_transcript(rec)
    return rec
