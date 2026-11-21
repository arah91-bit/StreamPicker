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
from dataclasses import dataclass, field

logger = logging.getLogger("stream-picker")

FFPROBE = shutil.which("ffprobe")
TIMEOUT = float(os.environ.get("FFPROBE_TIMEOUT", "20"))

# Lossless / high-bitrate audio codecs whose per-track rate the container often
# omits; estimated from channel count when ffprobe doesn't report a bit_rate.
_LOSSLESS = ("truehd", "mlp", "dts-hd", "dtshd", "flac", "alac", "pcm")

_LANG3_TO_2 = {
    "eng": "en", "ita": "it", "fra": "fr", "fre": "fr", "deu": "de",
    "ger": "de", "spa": "es", "rus": "ru", "jpn": "ja", "kor": "ko",
    "zho": "zh", "chi": "zh", "pol": "pl", "por": "pt", "nld": "nl",
    "dut": "nl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
    "tur": "tr", "ces": "cs", "cze": "cs", "hun": "hu", "ron": "ro",
    "rum": "ro", "tha": "th", "vie": "vi", "ind": "id", "hin": "hi",
    "heb": "he", "ara": "ar", "ukr": "uk", "tam": "ta", "tel": "te",
}


async def _terminate(proc) -> None:
    """Kill and reap an ffprobe child without leaking a zombie."""
    if proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await proc.wait()
    except Exception:
        pass


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
        await _terminate(proc)
        return None
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    except Exception:
        await _terminate(proc)
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


# ffprobe's color_transfer values that mean "this needs an HDR display".
# Everything else — bt709, bt470*, smpte170m, iec61966-2-1, unknown/absent —
# is SDR or unstated, and unstated must read as SDR: an untagged file plays
# correctly everywhere, so guessing HDR would demote good releases.
_PQ_TRANSFERS = {"smpte2084", "smpte st 2084", "pq"}
_HLG_TRANSFERS = {"arib-std-b67", "arib std b67", "hlg"}


# 10-bit AVC (High 10 Profile) is its own decode problem and has to be its own
# learnable attribute. Hardware decoders that handle AVC and HEVC Main 10
# without complaint routinely refuse it — no Shield decodes it at all — and it
# is common in anime releases, whose filenames often say nothing about it
# so a probe must identify the profile. Deliberately not generalised to other codecs: HEVC Main 10 is
# universally supported, so splitting its key would fragment what we learn for
# no gain.
_HI10_PIX = ("yuv420p10", "yuv422p10", "yuv444p10", "yuv420p12")


def _video_key(stream: dict) -> str:
    """Codec name, with the profile folded in where the profile is the risk."""
    codec = _norm_codec(stream.get("codec_name", ""))
    if codec != "h264":
        return codec
    pix = str(stream.get("pix_fmt") or "").lower()
    profile = str(stream.get("profile") or "").lower()
    if profile.startswith("high 10") or any(pix.startswith(p) for p in _HI10_PIX):
        return "h264-10bit"
    return codec


def _norm_transfer(value: str) -> str:
    """'pq' (HDR10/HDR10+/DV base), 'hlg', or '' for SDR and unknown."""
    v = (value or "").strip().lower()
    if v in _PQ_TRANSFERS:
        return "pq"
    if v in _HLG_TRANSFERS:
        return "hlg"
    return ""


@dataclass
class MediaDetails:
    """Everything one ffprobe pass over a file's track table tells us."""
    audio: list[str] = field(default_factory=list)   # normalized audio codecs
    video: str = ""                                  # normalized video codec
    secs: float = 0.0                                # declared duration
    langs: list[str] = field(default_factory=list)   # audio-track languages
    transfer: str = ""                               # "pq" / "hlg" / ""
    # Subtitle-track languages ("" for an untagged track). None means the
    # track table was never read — no video stream parsed — which is NOT the
    # same as an empty list: [] is a measured "this file carries no
    # switchable subtitles", the signature of a burned-in-subtitle master.
    subs: list[str] | None = None


async def media_info_of(target: str | bytes, timeout: float = 5.0
                        ) -> tuple[list[str], str, float, list[str], str]:
    """Audio codecs, video codec, duration, audio-track languages, and the
    video transfer function — the tuple view of media_details_of."""
    d = await media_details_of(target, timeout=timeout)
    return d.audio, d.video, d.secs, d.langs, d.transfer


async def media_details_of(target: str | bytes,
                           timeout: float = 5.0) -> MediaDetails:
    """Audio codecs, video codec, duration, audio-track languages, the video
    transfer function, and the subtitle tracks of a media file.

    `target` is a filesystem path or the first couple MB of the file as
    bytes — containers keep their track table (and, for MKV/faststart MP4,
    their declared duration) near the head, so a partial prefix parses fine.
    Missing language tags remain unknown rather than being guessed.

    The transfer function comes from the same ffprobe call as everything else,
    because it is the only *measured* answer to "is this HDR". Release names
    are claims: they carry "HDR" on SDR encodes and omit it on graded ones.
    """
    if not enabled():
        return MediaDetails()
    args = [FFPROBE, "-v", "error", "-of", "json",
            "-show_entries",
            "stream=codec_type,codec_name,color_transfer,profile,pix_fmt"
            ":stream_tags=language:format=duration"]
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
        await _terminate(proc)
        return MediaDetails()
    except asyncio.CancelledError:
        await _terminate(proc)
        raise
    except Exception:
        if "proc" in locals():
            await _terminate(proc)
        return MediaDetails()
    try:
        data = json.loads(out)
        streams = data.get("streams", [])
    except Exception:
        return MediaDetails()
    audio = [_norm_codec(s.get("codec_name", ""))
             for s in streams if s.get("codec_type") == "audio"]
    vstream = next((s for s in streams if s.get("codec_type") == "video"), None)
    video = _video_key(vstream) if vstream else ""
    transfer = _norm_transfer(next(
        (s.get("color_transfer", "") for s in streams
         if s.get("codec_type") == "video"), ""))
    try:
        secs = float(data.get("format", {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        secs = 0.0
    langs: list[str] = []
    subs: list[str] = []
    for stream in streams:
        kind = stream.get("codec_type")
        if kind not in ("audio", "subtitle"):
            continue
        raw = str((stream.get("tags") or {}).get("language") or "").lower()
        lang = _LANG3_TO_2.get(raw, raw if len(raw) == 2 else "")
        if kind == "subtitle":
            subs.append(lang)
        elif lang and lang not in langs:
            langs.append(lang)
    # Only a parsed video stream proves the track table was read. Without one
    # an empty subtitle list would mean "never looked", not "none there".
    return MediaDetails(audio=[a for a in audio if a], video=video, secs=secs,
                        langs=langs, transfer=transfer,
                        subs=subs if vstream else None)


async def codecs_of(target: str | bytes,
                    timeout: float = 5.0) -> tuple[list[str], str, float]:
    """Backward-compatible codec/duration view."""
    audio, video, secs, _langs, _transfer = await media_info_of(
        target, timeout=timeout)
    return audio, video, secs


async def shutdown() -> None:
    """Uniform lifecycle hook; children are reaped per operation."""
    return None
