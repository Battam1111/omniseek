"""Local ASR — transcribe the SPOKEN content the agent cannot hear itself.

Model choice was BENCHMARKED on real code-switched Chinese podcast audio (not just CER tables):
SenseVoice-Small (FunAudioLLM / Alibaba, via funasr) produced an accurate, punctuated transcript
with correct Mandarin + code-switched English (CEO / AI / GX), at RTF ~0.03 (≈38× realtime) on the
Apple-silicon GPU (MPS). whisper-large-v3 on the SAME clip HALLUCINATED (the notorious "请点赞订阅转发打赏"
loop) — Whisper is unreliable on Chinese audio that opens with music/intros, i.e. most podcasts.
SenseVoice also leads the Mandarin CER benchmarks (2.96% vs Whisper-large-v3 5.14%) and ships
built-in punctuation + a VAD pipeline (fsmn-vad) that chunks long audio for free.

Flow: resolve an audio source (小宇宙 enclosure / bilibili via yt-dlp / direct file) → imageio-ffmpeg
decode to a 16k mono WAV (robust, no PATH/codec-backend reliance) → SenseVoice (funasr, MPS) with VAD
for long-form → strip the model's audio-event / emotion tags → cache FOREVER (spoken content never
changes). youtube does NOT route here (its adapter already returns captions). ASR is heavy to load +
slow-ish on long media, so it is an explicit, agent-driven tool (omniseek_transcribe), never a broad sweep.

Time-range transcription (2026-06-10): start/duration slice the audio BEFORE ASR (ffmpeg -ss/-t;
on direct/enclosure URLs -ss uses HTTP range seeking, so only the slice region is even downloaded).
The unit of transcription becomes "the segment judged worth hearing" (e.g. one chapter from a
podcast's shownotes timestamps) instead of a whole 3-hour episode nobody reads.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import platform
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlparse

import anyio

from omniseek.core import _netguard, _optdep, asr_sched, cache, http, portal
from omniseek import asr_worker as _worker, where

logger = logging.getLogger(__name__)

# The model ids live with the worker that loads them (omniseek.asr_worker); the cache key and the engine
# label read them here.
_MODEL = _worker.MODEL
_VAD = _worker.VAD
_DIAR_MODEL = _worker.DIAR_MODEL
_SPK_MODEL = _worker.SPK_MODEL
# Human-readable names for the configured model ids, so every hint that tells a user or an agent which
# engine transcribes reads the id the code actually loads (OmniSeek R01: the bilibili hint said
# "Whisper" long after the engine became SenseVoice). An id missing here falls back to its own name.
_MODEL_LABELS = {"iic/SenseVoiceSmall": "SenseVoice-Small", "paraformer-zh": "Paraformer-zh"}
_SR = _worker.SR
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



def engine_label(diarize: bool = False) -> str:
    """The name of the local ASR engine omniseek_transcribe runs, derived from the configured model ids:
    the flat transcript path by default, the speaker-diarization pipeline with diarize=True."""
    def name(mid: str) -> str:
        return _MODEL_LABELS.get(mid) or mid.rsplit("/", 1)[-1]
    if diarize:
        return f"{name(_DIAR_MODEL)} + {name(_SPK_MODEL)}"
    return name(_MODEL)


# ── where recognition runs (2026-11-10) ───────────────────────────────────────────────────────
# Not in this process. The models load in a speech worker process (omniseek.asr_worker) that
# omniseek.core.asr_sched starts, feeds one job at a time and ends when it is idle, too big for another
# job, or has to be stopped. The measures that bounded the models inside OmniSeek (release the MPS cache
# after every generate, one generate at a time, one model family at a time, the idle unload) moved with
# them: the first three run in the worker, the idle unload became the worker's idle exit. What a loaded
# model leaves in a process (MPSGraph's per-shape heap, 1 to 2.5 GB after long files, which no unload
# gave back) now ends with the worker instead of needing a restart of OmniSeek.


# ── dependency check (runs BEFORE any audio is fetched) ─────────────────────────────────────────
# The 'asr' extra installs funasr + imageio-ffmpeg but NOT torch: the right torch build differs by
# platform (macOS MPS, Linux CUDA or CPU, Windows), so it is installed separately. funasr's own
# "requires PyTorch" error suggests a CUDA wheel index even on a Mac, and it only fires after the
# audio was downloaded and decoded. Checking up front lets one message name EVERYTHING missing,
# with the command for THIS platform and the disk the first call will need.
# Disk figures measured on OmniSeek host 2026-10-10 (~/.cache/modelscope/hub/models/iic):
# SenseVoiceSmall 897M + fsmn-vad 3.9M; diarize adds paraformer-zh 953M + ct-punc 1.1G + cam++ 28M.
_MODEL_DISK_NOTE = ("The first transcription downloads the SenseVoice model (about 0.9 GB, into "
                    f"{where.show(where.modelscope_cache())}); diarize=True downloads about 2.1 GB more on first use.")


def _has_cuda_gpu() -> bool:
    """Cheap, side-effect-free NVIDIA presence check (no torch needed: it is what is missing)."""
    return bool(shutil.which("nvidia-smi")) or os.path.exists("/proc/driver/nvidia/version")


def _torch_install_hint(system: Optional[str] = None, cuda: Optional[bool] = None) -> str:
    """The torch install command for THIS platform. macOS and GPU-less machines get the plain PyPI
    command; a CUDA wheel index is suggested only when an NVIDIA driver is actually present."""
    system = (system or platform.system()).lower()
    cuda = _has_cuda_gpu() if cuda is None else cuda
    plain = "pip install torch torchaudio"
    if system == "darwin":
        return f"{plain} (the macOS build uses the Apple GPU via MPS; about 0.5 GB)"
    if system == "linux" and not cuda:
        return (f"{plain} (no NVIDIA GPU found; the smaller CPU-only build is: {plain} "
                "--index-url https://download.pytorch.org/whl/cpu)")
    if cuda:
        default = ("the default Linux build already includes CUDA; "
                   if system == "linux" else "")
        return (f"{plain} ({default}NVIDIA GPU found; to match your driver's CUDA version use "
                f"{plain} --index-url https://download.pytorch.org/whl/cuXXX, with cuXXX "
                "from https://pytorch.org/get-started/locally/)")
    return plain


def _missing_asr_deps() -> list[str]:
    return [m for m in ("funasr", "imageio_ffmpeg", "torch", "torchaudio")
            if importlib.util.find_spec(m) is None]


def _asr_install_message(missing: list[str], system: Optional[str] = None,
                         cuda: Optional[bool] = None) -> str:
    """One actionable message for everything the transcription path lacks."""
    extra = [m for m in missing if m in ("funasr", "imageio_ffmpeg")]
    torch_missing = [m for m in missing if m in ("torch", "torchaudio")]
    steps = []
    if extra:
        steps.append("pip install 'omniseek[asr]'")
    if torch_missing:
        steps.append(_torch_install_hint(system, cuda))
    return (f"audio transcription is not installed (missing: {', '.join(missing)}). "
            f"Install: {'; then '.join(steps)}. {_MODEL_DISK_NOTE}")


def _model_cached(model_id: str) -> bool:
    root = os.environ.get("MODELSCOPE_CACHE") or str(where.modelscope_cache() / "hub")
    return any(os.path.isdir(os.path.join(root, sub, model_id)) for sub in ("models", ""))


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


def _workdir(ticket) -> Optional[str]:
    """The transcription's own temporary directory (removed when it ends, asr_sched.remove_workdir), or
    None for the process's temp directory."""
    return None if ticket is None or ticket.workdir is None else str(ticket.workdir)


def _run_proc(cmd: list, ticket=None) -> tuple:
    """Run a download or decode process the transcription's cancellation can kill: (returncode, stderr)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if ticket is not None:
        ticket.watch(proc)
    _, err = proc.communicate()
    if ticket is not None and ticket.cancelled.is_set():
        raise asr_sched.Cancelled()
    return proc.returncode, err


def _decode_to_wav(src: str, start_s: Optional[float] = None, dur_s: Optional[float] = None,
                   ticket=None) -> str:
    """ffmpeg-decode any audio file/URL to a temp 16k mono WAV; return its path (caller removes).

    start_s/dur_s slice the audio: -ss before -i is a fast input seek (HTTP range
    on remote URLs, so a 10-min slice of a 3-hour episode never downloads the rest)."""
    fd, path = tempfile.mkstemp(suffix=".wav", prefix="omniseek-asr-", dir=_workdir(ticket))
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
    returncode, stderr = _run_proc(cmd, ticket)
    # The bundled ffmpeg's TLS (macOS SecureTransport) intermittently fails to negotiate with some CDNs
    # (-9806 on cloudfront / anchor), surfacing two ways: a non-zero exit, OR exit 0 with an empty /
    # near-empty WAV. On a REMOTE url either way, fall back to a robust httpx (openssl) download of the
    # whole file, then decode it LOCALLY (file protocol, ffmpeg never touches TLS). The fast ffmpeg
    # range-seek stays the default for CDNs it handles fine (e.g. xyzcdn), so slice efficiency is kept.
    if scheme in ("http", "https") and (returncode != 0 or os.path.getsize(path) < _MIN_WAV_BYTES):
        _rm(path)
        dl = None
        try:
            fd2, dl = tempfile.mkstemp(suffix=".audio", prefix="omniseek-asr-dl-", dir=_workdir(ticket))
            os.close(fd2)
            from omniseek.core import http as _http  # lazy: asr stays import-light
            _http.download_to_file(src, dl, max_bytes=_AUDIO_DL_MAX_BYTES)
            if ticket is not None and ticket.cancelled.is_set():
                raise asr_sched.Cancelled()
            return _decode_to_wav(dl, start_s, dur_s, ticket)  # local path -> "file" proto, no ffmpeg TLS
        except asr_sched.Cancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"ffmpeg direct decode failed and robust-download fallback failed: {exc}") from exc
        finally:
            _rm(dl)
    if returncode != 0:
        try:
            os.remove(path)
        except OSError:
            pass
        raise RuntimeError(f"ffmpeg decode failed: {stderr.decode('utf-8', 'ignore')[:200]}")
    return path


def _models() -> dict:
    return {"model": _MODEL, "vad": _VAD, "diar": _DIAR_MODEL, "spk": _SPK_MODEL, "punc": _worker.PUNC_MODEL}


def _recognize(ticket, op: str, wav_path: str, audio_s: float, **kw):
    """One job on the speech worker (asr_sched.POOL): op "flat" gives the transcript, "segments" the
    per-VAD-segment [{start,end,text}] (SenseVoice emits no offsets, so the worker runs fsmn-vad
    standalone and recognizes the slices in one batch), "diar" [flat, [{start,end,text,speaker}],
    n_speakers] via Paraformer-zh + fsmn-vad + cam++ + ct-punc (all from modelscope, no HuggingFace
    token; SenseVoice emits no timestamps, which funasr's speaker attribution needs). Raises
    asr_sched.JobError or asr_sched.Cancelled."""
    return asr_sched.POOL.run(ticket, dict(op=op, wav=wav_path, models=_models(), **kw), audio_s)


def _xiaoyuzhou_audio(url: str) -> tuple[Optional[str], dict]:
    """Resolve a 小宇宙 episode page URL → direct audio enclosure URL (+ meta)."""
    try:
        r = http.direct("GET", url, headers={"User-Agent": _UA}, timeout=20, follow_redirects=True)
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
    """Sync entry for a caller on a thread: ``abilibili_playurl`` on the event loop (portal.call)."""
    return portal.call(abilibili_playurl, url)


async def abilibili_playurl(url: str) -> Optional[dict]:
    """Activated-session playurl for a bilibili video -> its DASH streams + the handles to fetch
    them. Runs the full browser-style bootstrap (homepage -> spi buvid3/4 -> synth _uuid/b_lsid ->
    bili_ticket -> ExClimbWuzhi activation -> WBI-signed playurl) in ONE httpx session (an AsyncHopClient: the declared gates and the redirect rule on every hop). Shared by
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
        async with http.AsyncHopClient(headers=base_headers, follow_redirects=True, timeout=30) as c:
            # b23.tv (or any non-BV URL) -> follow to the real video page for its BVID
            if bvid is None:
                r = await c.get(url)
                m = _BVID_RE.search(str(r.url)) or _BVID_RE.search(r.text)
                if not m:
                    logger.warning("bilibili: no BVID resolvable from %s", url)
                    return None
                bvid = m.group(0)

            await c.get("https://www.bilibili.com/")  # seeds buvid3 / b_nut
            spi = (await c.get("https://api.bilibili.com/x/frontend/finger/spi")).json()
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
            tj = (await c.post("https://api.bilibili.com/bapis/bilibili.api.ticket.v1.Ticket/GenWebTicket"
                               "?key_id=ec02&hexsign=%s&context[ts]=%d&csrf=" % (hexsign, t))).json()
            if tj.get("code") == 0:
                ck(c, "bili_ticket", tj["data"]["ticket"])
                ck(c, "bili_ticket_expires", str(t + tj["data"]["ttl"]))

            # ExClimbWuzhi gaia activation -- THE missing piece that flips playurl 412 -> 200.
            payload = {"3064": 1, "5062": str(ts), "03bf": "https://www.bilibili.com/",
                       "39c8": "333.1007.fp.risk", "6e7c": "878x1080",
                       "3c43": {"b8ce": _BILI_UA, "07a4": "zh-CN", "6aa9": "Asia/Shanghai"}}
            await c.post("https://api.bilibili.com/x/internal/gaia-gateway/ExClimbWuzhi",
                         json={"payload": json.dumps(payload)})

            vj = (await c.get("https://api.bilibili.com/x/web-interface/view",
                              params={"bvid": bvid})).json()
            data = vj.get("data") or {}
            cid = data.get("cid")
            if not cid:
                logger.warning("bilibili: no cid for %s (code %s)", bvid, vj.get("code"))
                return None

            nav = (await c.get("https://api.bilibili.com/x/web-interface/nav")).json()
            wi = nav["data"]["wbi_img"]
            orig = (wi["img_url"].rsplit("/", 1)[-1].split(".")[0]
                    + wi["sub_url"].rsplit("/", 1)[-1].split(".")[0])
            wbi_key = "".join(orig[i] for i in _BILI_WBI_TAB)[:32]

            query = _bili_wbi_signed_query({"bvid": bvid, "cid": cid, "fnval": 4048,
                                            "try_look": 1}, wbi_key)
            ref = "https://www.bilibili.com/video/%s" % bvid
            r = await c.get("https://api.bilibili.com/x/player/wbi/playurl?" + query,
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


def _bili_download(urls: list, cookies: dict, referer: str, dest: str, stop=None) -> bool:
    """Sync entry for a caller on a thread: ``_abili_download`` on the event loop (portal.call)."""
    return portal.call(_abili_download, urls, cookies, referer, dest, stop)


async def _abili_download(urls: list, cookies: dict, referer: str, dest: str, stop=None) -> bool:
    """Download the first working bilibili CDN url to ``dest`` (a DASH .m4s; pre-signed, but the
    CDN requires the video-page Referer + the activated cookies). True on success. ``stop`` (an Event)
    set during the download ends it (a cancelled transcription stops downloading)."""
    hdrs = {"User-Agent": _BILI_UA, "Referer": referer}
    for u in urls:
        try:
            async with http.adirect_stream("GET", u, headers=hdrs, cookies=cookies, timeout=120,
                                           follow_redirects=True) as resp:
                resp.raise_for_status()
                async with await anyio.open_file(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(262144):
                        if stop is not None and stop.is_set():
                            return False
                        await f.write(chunk)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("bilibili CDN url failed (%s): %s", str(u)[:60], exc)
    return False


def _bilibili_audio(url: str, ticket=None) -> tuple[Optional[str], dict]:
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
    tmpdir = tempfile.mkdtemp(prefix="omniseek-asr-dl-", dir=_workdir(ticket))
    path = os.path.join(tmpdir, "a.m4s")
    stop = None if ticket is None else ticket.cancelled
    if _bili_download(_bili_stream_urls(lo), pl["cookies"], pl["referer"], path, stop):
        return path, {"source": "bilibili", "title": pl.get("title"), "author": pl.get("author")}
    import shutil
    shutil.rmtree(tmpdir, ignore_errors=True)
    return None, {}


def _ytdlp_download(url: str, ticket=None) -> tuple[Optional[str], dict]:
    """Download bestaudio to a temp file via yt-dlp (non-bilibili video/podcast hosts — bilibili
    has its own activated-session path that yt-dlp structurally can't do; see _bilibili_audio).
    A real UA is the difference between working and blocked. Returns (path, meta); caller removes
    the file + its dir."""
    import yt_dlp
    tmpdir = tempfile.mkdtemp(prefix="omniseek-asr-dl-", dir=_workdir(ticket))

    def _hook(_d):  # a cancelled transcription stops downloading
        if ticket is not None and ticket.cancelled.is_set():
            raise asr_sched.Cancelled()

    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bestaudio/best", "outtmpl": os.path.join(tmpdir, "a.%(ext)s"),
            "postprocessors": [], "http_headers": {"User-Agent": _UA}, "progress_hooks": [_hook]}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if not f.endswith(".txt")]
        path = files[0] if files else None
        return path, {"source": (info.get("extractor") or "video").split(":")[0],
                      "title": info.get("title"), "author": info.get("uploader")}
    except Exception as exc:  # noqa: BLE001
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)   # a failed fetch left an empty download dir behind
        if ticket is not None and ticket.cancelled.is_set():
            raise asr_sched.Cancelled() from exc
        logger.warning("yt-dlp audio download failed %s: %s", url, exc)
        # Carry the real reason out. Swallowing it forced the caller to GUESS at a cause, and the
        # guess it printed ("unsupported host?") sent a reader looking for a routing bug when the
        # actual failure was a 403 from a stale yt-dlp.
        return None, {"error": str(exc).strip()[:200]}


def _rm(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
    d = os.path.dirname(path)
    if os.path.basename(d).startswith("omniseek-asr-dl-"):
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# 抖音: its videos were resolved through OmniSeek's logged-in douyin browser, which was withdrawn on
# 2026-10-20 (yt-dlp's Douyin extractor rejects even logged-in cookies, so there is no other way in).
# A douyin URL now gets this one sentence and nothing is fetched.
DOUYIN_UNSUPPORTED = "抖音视频暂不支持转写（登录浏览器已撤）"


def _remember_transcript(rec: dict) -> None:
    """Perception-produced text enters OmniSeek's MEMORY (2026-07-05): a transcript is text OmniSeek
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
        from omniseek.core.normalize import Document
        from omniseek.core.recall import writer
        writer.ingest_produced([Document(
            source="asr", source_id=sid, url=url, title=title, content=text,
            tags=["transcript"],
            metadata={"asr_model": rec.get("model"), "audio_seconds": rec.get("audio_seconds"),
                      "media_source": rec.get("source")},
        )])
    except Exception as exc:  # noqa: BLE001 — memory must never break perception
        logger.debug("transcript memory ingest skipped: %s", exc)


# ── single flight per cache key (incident 2026-10-11 14:44 to 15:02) ──────────────────────────
# omniseek_gather returns "warming" after its wait window while the transcription keeps running in the
# background; the caller re-issued the same 87-minute omniseek_transcribe three more times and each call
# fetched the audio again and queued another full run on the model, until memguard restarted OmniSeek
# with three transcriptions in flight and every result was lost. The cache only knows a key once a
# run FINISHES, so a repeat inside the run window is a miss. _inflight closes that window: the first
# caller for a key is the leader and starts it; every later caller for the same key, while it runs,
# is a follower that waits on the leader's job and never starts a second run. So at any moment at most
# ONE run exists per key.
#
# Distinct keys go through asr_sched: a machine-wide number of transcriptions at once (download, decode
# and recognition together), a memory admission before each starts, one recognition at a time on the
# speech worker. A run that cannot start at once is queued; its leader returns a queued receipt (its
# place, how to collect the result) and the run goes on in the background, so a later call with the
# same arguments attaches to it or reads its cached result.
_inflight: dict = {}
_inflight_lock = threading.Lock()
_runs_started = 0   # real (uncached) runs that got past admission in this process; status reads it


def _follow_wait_s() -> float:
    """How long a follower waits on the running leader before returning the running receipt
    (env OMNISEEK_ASR_FOLLOW_WAIT_S, default 30). Provisional: omniseek_gather's default wait_s is 60 s,
    so a follower issued inside a default gather returns its own explicit receipt (already running,
    no second run) before the gather window closes, instead of a generic warming. Calibrate from the
    log: if most followers return the receipt within seconds of the leader finishing, raise it."""
    try:
        return max(0.0, float(os.environ.get("OMNISEEK_ASR_FOLLOW_WAIT_S", "30")))
    except ValueError:
        return 30.0


class _Job:
    __slots__ = ("started", "audio_seconds", "done", "result", "ticket")

    def __init__(self, url: str):
        self.started = time.monotonic()
        self.audio_seconds = None
        self.done = threading.Event()
        self.result = None
        self.ticket = asr_sched.Ticket(url)

    @property
    def phase(self) -> str:
        return self.ticket.phase


def runs_started() -> int:
    """Real (uncached) transcription runs this process has started (past the machine-wide admission)."""
    return _runs_started


def _join_or_lead(ck, url):
    """Elect under _inflight_lock: (job, True, None) for the leader, (job, False, None) for a follower,
    (None, False, cached) when a run for ck finished after the caller's first cache read (the leader
    writes the cache BEFORE it leaves _inflight, so re-reading under the lock closes that gap)."""
    with _inflight_lock:
        job = _inflight.get(ck)
        if job is not None:
            return job, False, None
        cached = cache.get(ck)
        if cached is not None:
            return None, False, cached
        job = _Job(url)
        _inflight[ck] = job
        return job, True, None


def _finish(ck, job, rec, url) -> None:
    """Leader exit: publish the result to followers and free the key (always, even on a crash)."""
    with _inflight_lock:
        job.result = rec if rec is not None else {
            "url": url, "transcript": "", "error": "transcription crashed before returning a result"}
        if _inflight.get(ck) is job:
            del _inflight[ck]
    job.done.set()


def _limits_note() -> str:
    return (f"The machine runs at most {asr_sched.max_segments()} transcriptions at a time (download, "
            f"decode and recognition together), and starts none while the system's memory pressure is "
            f"critical.")


def _queued_receipt(job, url, position: int) -> dict:
    t = job.ticket
    why = (" It is waiting for memory to free up." if t.waiting_for == "memory" else "")
    return {"url": url, "transcript": "", "status": "queued", "queued": True,
            "queue_position": position, "transcriptions_in_flight": asr_sched.slots_busy(),
            "waiting_for": t.waiting_for or "slot",
            "note": (f"Queued: number {position} in line.{why} {_limits_note()} It will run without "
                     "you: call omniseek_transcribe again with the same arguments to attach to it; once it "
                     "has finished, the same call returns the result from the cache.")}


def _follow(job, url) -> dict:
    """A repeat call for a key that is already running: wait on that run, never start another."""
    if job.done.wait(_follow_wait_s()):
        return {**job.result, "joined": True}
    pos = asr_sched.SCHED.position(job.ticket)
    if pos:
        return {**_queued_receipt(job, url, pos), "already_running": True}
    running = round(time.monotonic() - job.started, 1)
    with _inflight_lock:
        n = len(_inflight)
    rec = {"url": url, "transcript": "", "status": "running", "already_running": True,
           "running_seconds": running, "phase": job.phase, "transcriptions_in_flight": n,
           "note": (f"This transcription is already running (started {running:.0f} s ago). Calling "
                    "again will NOT start a second run: a repeat call with the same arguments attaches "
                    "to this one, and once it finishes returns the cached result instantly. "
                    + _limits_note())}
    if job.audio_seconds is not None:
        rec["audio_seconds"] = job.audio_seconds
    return rec


def transcribe_url(url: str, language: Optional[str] = None,
                   start=None, duration=None, segments: bool = False, diarize: bool = False,
                   speakers: Optional[int] = None, caller: Optional[str] = None) -> dict:
    """Resolve audio for `url` → SenseVoice transcript. Cached forever. See omniseek_transcribe for fields.

    start/duration (seconds or MM:SS / HH:MM:SS) transcribe only that slice — the
    chapter-from-shownotes pattern for long episodes. segments=True ALSO returns a per-VAD-segment
    [{start,end,text}] list (seconds) so a no-shownote transcript is navigable / time-citable; the flat
    ``transcript`` is unchanged either way.

    diarize=True switches to the WHO-said-what path (Paraformer-zh + cam++, zh-focused): ``segments``
    become [{start,end,text,speaker}] and ``speakers`` carries the distinct-speaker count. It is a
    SEPARATE ASR pass (Paraformer, not SenseVoice) so the flat ``transcript`` is the diarized text
    joined; use it for interviews / multi-host podcasts where speaker turns matter. Pass ``speakers=N``
    (the known head-count) to pin cam++'s cluster count; leaving it None auto-estimates (unstable on
    short audio).

    caller names who asked (the job behind the request; asr_sched.cancel stops what a caller asked for).

    Single flight: while a run for the same arguments is in progress, a repeat call never starts a
    second one. It waits up to _follow_wait_s() for that run and returns its result (``joined``), or
    returns a running receipt ({status: "running", already_running: True, note, ...}, transcript "").
    Over the machine-wide limit (or short of memory) a new run is queued and the call returns a queued
    receipt ({status: "queued", queue_position, note, ...}, transcript ""); the run goes on without it."""
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
    if host == "douyin.com" or host.endswith(".douyin.com"):
        return {"url": url, "error": DOUYIN_UNSUPPORTED, "transcript": ""}
    # SSRF guard: transcribe_url takes an AGENT-controlled URL and the yt-dlp else-branch below
    # hands it straight to yt-dlp (a SEPARATE egress from the ffmpeg _decode_to_wav guard). Refuse
    # an SSRF-class http(s) URL here so no branch can reach a loopback/private host (127.0.0.1:9222
    # CDP). Non-http schemes are already rejected by _decode_to_wav's own guard.
    if scheme in ("http", "https"):
        _blk = _netguard.security_block_reason(url)
        if _blk:
            return {"url": url, "error": f"refused: {_blk}", "transcript": ""}

    missing = _missing_asr_deps()
    if missing:  # fail BEFORE fetching audio, with one message naming everything to install
        msg = _asr_install_message(missing)
        logger.warning("asr unavailable: %s", msg)
        return {"url": url, "error": msg, "transcript": ""}

    job, leader, cached = _join_or_lead(ck, url)
    if cached is not None:  # a run for this key finished between the first cache read and the election
        return {**cached, "cached": True}
    job.ticket.add_caller(caller)
    if not leader:
        return _follow(job, url)
    position = asr_sched.SCHED.submit(job.ticket)
    ctx = contextvars.copy_context()

    def _lead() -> None:
        rec = None
        try:
            rec = _transcribe_uncached(url, language, start_s, dur_s, segments, diarize, speakers, ck,
                                       host, job)
        except Exception as exc:  # noqa: BLE001 -- the result is the error, never a lost thread
            logger.warning("asr run %d failed %s: %s", job.ticket.id, url, exc)
            rec = {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "transcript": ""}
        finally:
            _finish(ck, job, rec, url)

    threading.Thread(target=ctx.run, args=(_lead,), name=f"asr-run-{job.ticket.id}", daemon=True).start()
    if position:
        return _queued_receipt(job, url, position)
    job.done.wait()
    return job.result


def _cancelled_rec(url, job) -> dict:
    who = ", ".join(sorted(job.ticket.ever_callers)) or "its caller"
    return {"url": url, "transcript": "", "cancelled": True,
            "error": f"cancelled: the transcription was stopped on request of {who}; nothing was cached"}


def _transcribe_uncached(url, language, start_s, dur_s, segments, diarize, speakers, ck, host,
                         job) -> dict:
    """The real run behind transcribe_url: wait for admission, fetch, decode, recognize, cache. Only the
    single-flight leader for ``ck`` calls this, so it runs at most once per key at any moment."""
    def _host_is(domain: str) -> bool:  # SUFFIX match, not substring: xiaoyuzhoufm.com.evil.net must NOT route
        return host == domain or host.endswith("." + domain)

    global _runs_started
    t = job.ticket
    if not asr_sched.SCHED.wait_admitted(t):
        return _cancelled_rec(url, job)
    t_wait = time.monotonic() - t.t_submit
    with _inflight_lock:
        _runs_started += 1
    t0 = time.time()
    ytmp = wav = None
    try:
        t.workdir = Path(tempfile.mkdtemp(prefix=f"omniseek-asr-{t.id}-"))
        if _host_is("xiaoyuzhoufm.com"):
            au, meta = _xiaoyuzhou_audio(url)
            if not au:
                return {"url": url, "error": "could not resolve 小宇宙 audio", "transcript": ""}
            wav = _decode_to_wav(au, start_s, dur_s, t)
        elif _host_is("bilibili.com") or _host_is("b23.tv"):
            ytmp, meta = _bilibili_audio(url, t)  # own activated session — yt-dlp 412s on bilibili
            if t.cancelled.is_set():
                raise asr_sched.Cancelled()
            if not ytmp:
                return {"url": url, "error": "could not resolve bilibili audio (anti-crawler 412?)", "transcript": ""}
            wav = _decode_to_wav(ytmp, start_s, dur_s, t)
        elif url.lower().split("?")[0].endswith(_AUDIO_EXT):
            meta = {"source": "audio"}
            wav = _decode_to_wav(url, start_s, dur_s, t)
        else:  # other video/podcast hosts → yt-dlp download (robust)
            ytmp, meta = _ytdlp_download(url, t)
            if not ytmp:
                why = (meta or {}).get("error")
                return {"url": url, "transcript": "",
                        "error": f"could not resolve audio for {host or url}: {why}" if why
                        else f"could not resolve audio for {host or url} (no audio stream found)"}
            wav = _decode_to_wav(ytmp, start_s, dur_s, t)
        _rm(ytmp)   # the decoded wav is all recognition needs; give the download's disk back now
        ytmp = None
        audio_secs = round(os.path.getsize(wav) / (_SR * 2))  # 16-bit mono
        job.audio_seconds = audio_secs
        t_fetch = time.time() - t0
        diar_segs, n_spk, seg = [], 0, None
        if diarize:  # WHO-said-what: the Paraformer diarization model gives BOTH the flat text + speaker segments
            text, diar_segs, n_spk = _recognize(t, "diar", wav, audio_secs, speakers=speakers)
        else:
            text = _recognize(t, "flat", wav, audio_secs, language=language)
            if segments:  # compute while the wav still exists
                seg = _recognize(t, "segments", wav, audio_secs, language=language)
    except asr_sched.Cancelled:
        return _cancelled_rec(url, job)
    except asr_sched.JobError as exc:
        logger.warning("asr failed %s: %s", url, exc)
        return {"url": url, "error": str(exc), "transcript": ""}
    except Exception as exc:  # noqa: BLE001
        logger.warning("asr failed %s: %s", url, exc)
        return {"url": url, "error": f"{type(exc).__name__}: {str(exc)[:200]}", "transcript": ""}
    finally:
        _rm(wav)
        _rm(ytmp)
        asr_sched.remove_workdir(t)
        asr_sched.SCHED.release(t)

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
    logger.info("asr run %d: %d s audio, queued %.1f s, fetched in %.1f s, done in %.1f s", t.id,
                audio_secs, t_wait, t_fetch, rec["asr_seconds"])
    cache.set(ck, rec, ttl=_TTL)
    _remember_transcript(rec)
    return rec
