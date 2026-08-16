"""Video keyframes -> in-band images: the VISUAL half of omniseek_transcribe.

omniseek_transcribe gives OmniSeek a video's spoken WORDS; this gives it the PICTURE -- the slides,
diagrams, on-screen code, charts, and UI demos a talk / lecture / explainer carries that audio
alone drops (the visual-track gap: video used to be audio-only). It mirrors the document image
path (docreader.view_images): OmniSeek RENDERS the pixels (samples frames), the agent's own
vision reads what they MEAN.

Resolve -> a local video file:
  * bilibili / b23.tv: the activated playurl session (asr.bilibili_playurl), lowest-res DASH
    VIDEO stream -- the visual sibling of asr._bilibili_audio (yt-dlp 412s bilibili).
  * everything else (youtube / slideslive / any yt-dlp host): metadata probe + a full video-only
    download (<=480p, single stream so yt-dlp needs no ffmpeg).
Then sample frames: SCENE-detect first (catch slide/cut changes -- the right frames for a talk),
falling back to EVEN spacing when there are too few cuts (smooth animation / continuous video).
Frames are downscaled and tiled into one labeled contact sheet (timestamp per cell) for cheap
triage. The frame extraction uses the imageio ffmpeg binary directly (no PATH dependency).
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_MAX_DIM = 1456          # match docreader._VIEW_MAX_DIM (in-band downscale long edge)
_DEFAULT_N = 12
_MAX_N = 24
_SCENE_THRESH = 0.27     # ffmpeg scene-change score above which a frame is a "cut" (slide change)


def _ts(seconds: float) -> str:
    """Seconds -> a compact clock label (M:SS or H:MM:SS) for a frame's timestamp."""
    s = int(max(seconds, 0))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _probe_seconds(ff: str, path: str) -> float:
    """Duration of a local video file via ffmpeg's stderr banner (no ffprobe dependency)."""
    try:
        out = subprocess.run([ff, "-i", path], capture_output=True, text=True, timeout=30).stderr
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if m:
            h, mn, s = m.groups()
            return int(h) * 3600 + int(mn) * 60 + float(s)
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _even_frames(ff, vid, s0, window, n, max_dim, outdir):
    """N evenly-spaced frames via fast `-ss` seeks (exact timestamps). Robust on any video."""
    got = []
    for i in range(n):
        t = s0 + (i + 0.5) * window / n
        out = os.path.join(outdir, f"e_{i:03d}.png")
        subprocess.run([ff, "-nostdin", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", vid,
                        "-frames:v", "1", "-vf", f"scale={max_dim}:-1:flags=lanczos", out],
                       capture_output=True, timeout=60)
        if os.path.exists(out) and os.path.getsize(out) > 0:
            got.append((t, out))
    return got


def _scene_frames(ff, vid, s0, window, n, max_dim, outdir):
    """Frames at scene CHANGES (slide/cut detection) -- the right frames for a talk/lecture.
    Returns [] when there are too few cuts (smooth animation / continuous video) so the caller
    falls back to even sampling. Timestamps are approximate (evenly assigned across the kept scene
    frames in order): the value is catching the right FRAMES, not exact times."""
    subprocess.run([ff, "-nostdin", "-loglevel", "error", "-ss", f"{s0:.2f}", "-t", f"{window:.2f}",
                    "-i", vid,
                    "-vf", f"select=gt(scene\\,{_SCENE_THRESH}),scale={max_dim}:-1:flags=lanczos",
                    "-vsync", "vfr", "-frames:v", str(_MAX_N * 2),
                    os.path.join(outdir, "s_%03d.png")], capture_output=True, timeout=180)
    imgs = sorted(f for f in os.listdir(outdir) if f.startswith("s_") and f.endswith(".png"))
    if len(imgs) < max(3, n // 3):  # too few cuts -> not a slide/cut video; even-sample instead
        for f in imgs:
            try:
                os.remove(os.path.join(outdir, f))
            except OSError:
                pass
        return []
    if len(imgs) > n:  # many cuts -> keep n spread evenly across them
        idx = sorted({round(i * (len(imgs) - 1) / (n - 1)) for i in range(n)})
        imgs = [imgs[j] for j in idx]
    k = len(imgs)
    return [(s0 + (i + 0.5) * window / k, os.path.join(outdir, f)) for i, f in enumerate(imgs)]


def _result(pairs, max_dim, title, total, s0, window, sampling, requested) -> dict:
    """Build the labeled contact sheet + manifest from (timestamp, frame-path) pairs.
    sampling is "scene" (frames at detected cuts) or "even" (evenly spaced); requested is the
    clamped n the caller asked for, so the caller can report when scene-detect kept fewer."""
    from omniseek.core import docreader
    cells = []
    for t, p in pairs:
        with open(p, "rb") as fh:
            cells.append({"data": docreader._downscale_png(fh.read(), max_dim),
                          "section_label": _ts(t), "name": os.path.basename(p)})
    sheet = docreader._contact_sheet(cells)
    return {
        "sheet": sheet, "shown": len(cells), "requested": requested, "sampling": sampling,
        "manifest": [{"idx": i + 1, "section_label": c["section_label"]}
                     for i, c in enumerate(cells)],
        "title": title, "duration_total": total or None,
        "start_seconds": round(s0), "window_seconds": round(window),
    }


def _bilibili_video_file(url: str, tmp: str):
    """Download bilibili's lowest-res DASH VIDEO stream via the activated playurl session (the
    visual sibling of asr._bilibili_audio). Returns (path, total_seconds, title) or None."""
    from omniseek.core import asr
    pl = asr.bilibili_playurl(url)
    if not pl:
        return None
    video = pl["dash"]["video"]
    if not video:
        logger.warning("bilibili: no video stream for %s", pl.get("bvid"))
        return None
    lo = min(video, key=lambda v: v.get("bandwidth", 0))  # lowest res is plenty for frames
    dest = os.path.join(tmp, "v.m4s")
    if not asr._bili_download(asr._bili_stream_urls(lo), pl["cookies"], pl["referer"], dest):
        return None
    return dest, (pl.get("duration") or 0), pl.get("title")


def _ytdlp_video_file(ff: str, url: str, tmp: str):
    """Metadata probe + full video-only download (<=480p) for any yt-dlp host. Returns
    (path, total_seconds, title) or None."""
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as y:
        meta = y.extract_info(url, download=False) or {}
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True,
            "format": "bv*[height<=480]/b[height<=480]/b", "outtmpl": os.path.join(tmp, "v.%(ext)s")}
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    files = [os.path.join(tmp, f) for f in os.listdir(tmp)
             if f.startswith("v.") and os.path.isfile(os.path.join(tmp, f)) and not f.endswith(".part")]
    if not files:
        return None
    return max(files, key=os.path.getsize), (meta.get("duration") or 0), meta.get("title")


def video_frames(url: str, start=None, duration=None, n: int = _DEFAULT_N,
                 max_dim: int = _MAX_DIM) -> dict:
    """Resolve a video + sample N frames -> a labeled contact-sheet PNG for in-band delivery.

    Returns {"sheet": <png bytes>, "shown", "manifest", "title", "duration_total", ...} (server
    wraps the sheet as an MCP Image), or {"error": ...} on failure."""
    from omniseek.core import _optdep
    imageio_ffmpeg = _optdep.require("imageio_ffmpeg", "asr")

    from omniseek.core.asr import _parse_ts

    n = max(1, min(int(n or _DEFAULT_N), _MAX_N))
    try:
        s0 = _parse_ts(start) or 0.0
        dur_req = _parse_ts(duration)
    except ValueError as exc:
        return {"error": str(exc)}

    # SSRF guard: video_frames takes an AGENT-controlled URL and hands it to yt-dlp / a session
    # helper, a separate egress from the mainline http guard. Refuse an SSRF-class http(s) URL so
    # no branch can reach a loopback/private host (127.0.0.1:9222 CDP).
    from omniseek.core import _netguard
    if (urlparse(url).scheme or "").lower() in ("http", "https"):
        _blk = _netguard.security_block_reason(url)
        if _blk:
            return {"error": f"refused: {_blk}"}

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    tmp = tempfile.mkdtemp(prefix="omniseek-vframes-")
    try:
        host = (urlparse(url).hostname or "").lower()
        if host == "bilibili.com" or host.endswith(".bilibili.com") \
                or host == "b23.tv" or host.endswith(".b23.tv"):
            res = _bilibili_video_file(url, tmp)
            if not res:
                return {"error": "bilibili video resolve failed (activation / anti-crawler / no video stream?)"}
        else:
            res = _ytdlp_video_file(ff, url, tmp)
            if not res:
                return {"error": "no video downloaded (host unsupported / blocked / no video stream?)"}
        vid, total, title = res

        if not total:
            total = _probe_seconds(ff, vid)
        window = dur_req if dur_req is not None else ((total - s0) if total else 0)
        if not window or window <= 0:
            window = float(n * 5)  # last-resort cadence when duration is unknowable

        outdir = os.path.join(tmp, "frames")
        os.makedirs(outdir)
        pairs = _scene_frames(ff, vid, s0, window, n, max_dim, outdir)
        sampling = "scene"
        if not pairs:
            pairs = _even_frames(ff, vid, s0, window, n, max_dim, outdir)
            sampling = "even"
        if not pairs:
            return {"error": "ffmpeg produced no frames from the video"}
        return _result(pairs, max_dim, title, total, s0, window, sampling, n)
    except Exception as exc:  # noqa: BLE001 -- any failure degrades to the error dict
        logger.warning("video_frames failed %s: %s", url, exc)
        return {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
