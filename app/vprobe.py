"""Real video-bitrate probe via ffprobe — slow picker only, top candidates only.

Overall bitrate (file size ÷ runtime) is a poor quality signal: a file bloated
with ten audio dubs has a high *overall* bitrate but its *video* may be
mediocre, and an over-compressed or upscaled "2160p" is starved no matter how
you count. This reads the container with ffprobe and isolates the video stream's
bitrate — the video stream's own bit_rate when the container carries it, else
the overall bitrate minus the (reported or estimated) audio tracks. It reads
container metadata, not the whole file, so it's cheap enough to run on a handful
of the best candidates. No-ops cleanly when ffprobe isn't installed.
"""

import asyncio
import json
import logging
import os
import shutil

logger = logging.getLogger("stream-picker")

FFPROBE = shutil.which("ffprobe")
TIMEOUT = float(os.environ.get("FFPROBE_TIMEOUT", "20"))

# Lossless / high-bitrate audio codecs whose per-track rate the container often
# omits; estimated from channel count when ffprobe doesn't report a bit_rate.
_LOSSLESS = ("truehd", "mlp", "dts-hd", "dtshd", "flac", "alac", "pcm")


def enabled() -> bool:
    return FFPROBE is not None


def _audio_estimate(codec: str | None, channels: int | None) -> float:
    c = (codec or "").lower()
    ch = channels or 2
    if any(k in c for k in _LOSSLESS):
        return ch * 600_000
    if "eac3" in c or "e-ac-3" in c:
        return ch * 128_000
    if c in ("ac3", "ac-3"):
        return ch * 96_000
    if "dts" in c:
        return 1_509_000
    if c in ("aac", "opus", "vorbis", "mp3"):
        return ch * 64_000
    return ch * 128_000


async def _one(url: str, overall: float | None) -> float | None:
    try:
        proc = await asyncio.create_subprocess_exec(
            FFPROBE, "-v", "error", "-of", "json",
            "-show_format", "-show_streams", url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    except Exception:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return None
    try:
        data = json.loads(out)
    except Exception:
        return None
    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return None
    # Best case: the container carries the video stream's own bitrate.
    try:
        if video.get("bit_rate"):
            return float(video["bit_rate"])
    except (TypeError, ValueError):
        pass
    # Otherwise: overall minus the audio tracks (reported or estimated).
    try:
        total = float(data.get("format", {}).get("bit_rate") or 0) or overall
    except (TypeError, ValueError):
        total = overall
    if not total:
        return None
    audio = 0.0
    for s in streams:
        if s.get("codec_type") != "audio":
            continue
        try:
            audio += (float(s["bit_rate"]) if s.get("bit_rate")
                      else _audio_estimate(s.get("codec_name"), s.get("channels")))
        except (TypeError, ValueError):
            audio += _audio_estimate(s.get("codec_name"), s.get("channels"))
    video_bps = total - audio
    return video_bps if video_bps > 0 else None


async def video_bitrates(
        url_overall: list[tuple[str, float | None]]) -> list[float | None]:
    """Video bitrate (bps) for each (url, overall_bps) pair, or None on failure.
    `overall_bps` is the size÷runtime fallback used when the container doesn't
    expose a video bit_rate and ffprobe can't compute a format bitrate."""
    if not enabled() or not url_overall:
        return [None] * len(url_overall)
    return await asyncio.gather(*[_one(u, o) for u, o in url_overall])


# ── codec identification (decode-compatibility learning) ────────────────────

def _norm_codec(name: str) -> str:
    """Normalize ffprobe codec names into stable attribute names: every
    pcm_s24le/pcm_f32be variant is 'pcm' for compatibility purposes."""
    n = (name or "").lower()
    return "pcm" if n.startswith("pcm") else n


async def codecs_of(target: str | bytes,
                    timeout: float = 5.0) -> tuple[list[str], str]:
    """(audio codec names, video codec name) of a media file. `target` is a
    filesystem path or the first couple MB of the file as bytes — containers
    keep their track table near the head, so a partial prefix parses fine for
    MKV and faststart MP4. ([], \"\") when ffprobe is missing or can't tell."""
    if not enabled():
        return [], ""
    args = [FFPROBE, "-v", "error", "-of", "json",
            "-show_entries", "stream=codec_type,codec_name"]
    stdin = None
    if isinstance(target, bytes):
        args.append("pipe:0")
        stdin = asyncio.subprocess.PIPE
    else:
        args.append(target)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=stdin, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(
            proc.communicate(input=target if stdin else None), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return [], ""
    except Exception:
        return [], ""
    try:
        streams = json.loads(out).get("streams", [])
    except Exception:
        return [], ""
    audio = [_norm_codec(s.get("codec_name", ""))
             for s in streams if s.get("codec_type") == "audio"]
    video = next((_norm_codec(s.get("codec_name", "")) for s in streams
                  if s.get("codec_type") == "video"), "")
    return [a for a in audio if a], video
