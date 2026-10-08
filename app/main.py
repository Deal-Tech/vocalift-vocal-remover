from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel


BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
RESULT_DIR = DATA_DIR / "results"

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
RESULT_TTL_HOURS = int(os.getenv("RESULT_TTL_HOURS", "24"))
MAX_YOUTUBE_MINUTES = int(os.getenv("MAX_YOUTUBE_MINUTES", "30"))
# TikTok caps uploads well below YouTube, so a lower ceiling here catches a
# mistyped link sooner without ever rejecting a real post.
MAX_TIKTOK_MINUTES = int(os.getenv("MAX_TIKTOK_MINUTES", "15"))
# Vocal-specialist checkpoint used by the official htdemucs_ft ensemble.
# It produces the same fine-tuned vocal stem without running the three models
# specialized for drums, bass, and other sources.
MODEL_NAME = os.getenv("DEMUCS_MODEL", "04573f0d")
MODEL_LABEL = (
    "Hybrid HTDemucs FT + Kim Vocal 2"
    if MODEL_NAME == "04573f0d"
    else MODEL_NAME
)
REFINEMENT_MODEL = os.getenv("VOCAL_REFINEMENT_MODEL", "Kim_Vocal_2.onnx")

# Shared STFT grid for every spectral stage. 4096/1024 at 44.1 kHz gives ~93 ms
# windows with 23 ms hops: fine enough to follow a vocal phrase, coarse enough
# that the masks stay smooth instead of chirping.
STFT_N_FFT = 4096
STFT_HOP = 1024

# --- Instrumental cleanup ---------------------------------------------------
# The instrumental is rebuilt from the mixture instead of being whatever the
# vocal model left behind, so these knobs decide how much of the mixture is
# treated as vocal. All of them are DSP-only: no extra separation pass runs.
#
# How far the residual may be ducked where a second model insists there is
# still vocal there. Floored, because the two models disagreeing is not proof.
INSTRUMENTAL_BLEED_FLOOR = float(os.getenv("INSTRUMENTAL_BLEED_FLOOR", "0.25"))
# A reverb tail is the vocal convolved with the room, so it is a linear
# function of the vocal the separator already gave us: the filter can be fitted
# from the audio and the tail subtracted with the correct phase. Taps set how
# far back that filter reaches, at ~23 ms per tap.
INSTRUMENTAL_TAIL_TAPS = int(os.getenv("INSTRUMENTAL_TAIL_TAPS", "14"))
# Ridge term on the fit. Vocal stems carry some instrument bleed, and without
# this the filter would happily "predict" music from that bleed and subtract it.
INSTRUMENTAL_TAIL_RIDGE = float(os.getenv("INSTRUMENTAL_TAIL_RIDGE", "0.05"))

# How much of the fitted tail is actually subtracted. 1 cancels it fully, 0
# disables the stage.
INSTRUMENTAL_TAIL_STRENGTH = float(os.getenv("INSTRUMENTAL_TAIL_STRENGTH", "1.0"))
# Lower bound of the conservative consensus mask on the vocal stem itself.
VOCAL_MASK_FLOOR = float(os.getenv("VOCAL_MASK_FLOOR", "0.22"))

ULTRA_ENSEMBLE_PRESET = "vocal_clean"
ULTRA_MODEL_LABEL = "RoFormer Vocal Clean (Revive V2 + Kim FT2 Bleedless)"
MODEL_DIR = DATA_DIR / "models"
AUDIO_SEPARATOR_CLI = Path(sys.executable).parent / (
    "audio-separator.exe" if sys.platform == "win32" else "audio-separator"
)
ALLOWED_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}
# TikTok hands out three shapes of link: the full one from a browser, the short
# one the share sheet produces, and the /t/ redirector. yt-dlp resolves the
# redirects itself, so all three only need to be recognised here.
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
}
PLATFORM_LABELS = {"youtube": "YouTube", "tiktok": "TikTok"}
# Every YouTube video ID is exactly 11 characters from this alphabet.
YOUTUBE_VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
# Path prefixes under which the second segment is a video ID.
YOUTUBE_ID_PATHS = {"shorts", "live", "embed", "v"}
# yt-dlp colours its errors; the codes have to go before the text is shown.
ANSI_CODES = re.compile(r"\x1b\[[0-9;]*m")
# Keeps results across restarts
JOB_RECORD_NAME = "job.json"
JOB_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
RESULT_PATH_KEYS = ("vocals", "instrumental", "vocals_mp3", "instrumental_mp3")
FRONTEND_URL = os.getenv("VOCALIFT_FRONTEND_URL", "http://127.0.0.1:5173")

for directory in (UPLOAD_DIR, RESULT_DIR, MODEL_DIR):
    directory.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Vocal Remover", version="1.0.0")
logger = logging.getLogger(__name__)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="demucs")
jobs: dict[str, dict[str, Any]] = {}
jobs_lock = threading.Lock()


class MediaUrlRequest(BaseModel):
    url: str
    quality_mode: Literal["balanced", "ultra"] = "balanced"


def _find_ffmpeg() -> bool:
    """Find Winget's FFmpeg even when the current terminal has stale PATH data."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True

    local_app_data = os.getenv("LOCALAPPDATA")
    if not local_app_data:
        return False

    package_root = Path(local_app_data) / "Microsoft" / "WinGet" / "Packages"
    if not package_root.exists():
        return False

    for ffmpeg in package_root.glob("Gyan.FFmpeg*/*/bin/ffmpeg.exe"):
        if (ffmpeg.parent / "ffprobe.exe").exists():
            os.environ["PATH"] = str(ffmpeg.parent) + os.pathsep + os.environ.get("PATH", "")
            return True
    return False


FFMPEG_AVAILABLE = _find_ffmpeg()
FFMPEG_EXE = shutil.which("ffmpeg") or "ffmpeg"


def _update_job(job_id: str, **values: Any) -> None:
    with jobs_lock:
        if job_id in jobs:
            if "progress" in values and values.get("status", jobs[job_id]["status"]) != "failed":
                values["progress"] = max(jobs[job_id]["progress"], values["progress"])
            jobs[job_id].update(values)


def _public_job(job: dict[str, Any]) -> dict[str, Any]:
    public = {
        "id": job["id"],
        "status": job["status"],
        "progress": job["progress"],
        "message": job["message"],
        "filename": job["filename"],
        "quality_mode": job.get("quality_mode", "balanced"),
        "source": job.get("source", "file"),
    }
    if job["status"] == "completed":
        # Prefer MP3 for the players, falling back to the completed WAV when
        # encoding the listening copy failed. Offer only successful downloads.
        public["files"] = {
            stem: (
                f'/api/jobs/{job["id"]}/files/{stem}'
                if job.get(_stem_key(stem, "mp3"))
                else f'/api/jobs/{job["id"]}/files/{stem}?format=wav'
            )
            for stem in ("vocals", "instrumental")
        }
        public["downloads"] = {
            stem: {
                audio_format: {
                    "url": f'/api/jobs/{job["id"]}/files/{stem}?format={audio_format}',
                    "bytes": _file_size(job.get(_stem_key(stem, audio_format))),
                }
                for audio_format in ("wav", "mp3")
                if job.get(_stem_key(stem, audio_format))
            }
            for stem in ("vocals", "instrumental")
        }
        public["analysis"] = job.get("analysis")
        public["refinement_applied"] = job.get("refinement_applied", False)
        public["separation_label"] = job.get("separation_label", MODEL_LABEL)
        public["warnings"] = job.get("warnings", [])
    return public


def _stem_key(stem: str, audio_format: str) -> str:
    """Where a rendered stem is kept on the job record."""
    return stem if audio_format == "wav" else f"{stem}_mp3"


def _save_job_record(job_id: str, result_root: Path) -> None:
    """Persist finished job beside stems."""
    with jobs_lock:
        record = dict(jobs.get(job_id) or {})
    for key in RESULT_PATH_KEYS:
        if record.get(key):
            record[key] = Path(record[key]).relative_to(result_root).as_posix()
    try:
        (result_root / JOB_RECORD_NAME).write_text(json.dumps(record), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        logger.warning("Could not save the record of job %s.", job_id, exc_info=True)


def _find_job(job_id: str) -> dict[str, Any] | None:
    """Job copy from memory or disk."""
    with jobs_lock:
        job = jobs.get(job_id)
        if job is not None:
            return job.copy()
    if not JOB_ID_PATTERN.fullmatch(job_id):
        return None
    result_root = RESULT_DIR / job_id
    try:
        record = json.loads((result_root / JOB_RECORD_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for key in RESULT_PATH_KEYS:
        if record.get(key):
            record[key] = result_root / record[key]
    with jobs_lock:
        return jobs.setdefault(job_id, record).copy()


def _file_size(path: Any) -> int | None:
    try:
        return Path(path).stat().st_size
    except (OSError, TypeError):
        return None


def _remove_intermediate(path: Path) -> None:
    """Cleanup must not turn usable audio output into a failed job."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove intermediate audio %s.", path, exc_info=True)


def _safe_download_name(original_name: str, suffix: str, extension: str) -> str:
    stem = Path(original_name).stem
    stem = re.sub(r"[^\w .-]", "_", stem, flags=re.UNICODE).strip(" ._") or "audio"
    return f"{stem}_{suffix}.{extension}"


def _cleanup_old_files() -> None:
    cutoff = time.time() - RESULT_TTL_HOURS * 3600
    for root in (UPLOAD_DIR, RESULT_DIR):
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
            except OSError:
                pass


def _detect_platform(host: str, path: str) -> str | None:
    """Which site a pasted link belongs to, or None if it is neither."""
    if host in YOUTUBE_HOSTS:
        return "youtube"
    if host in TIKTOK_HOSTS:
        # A bare profile link (tiktok.com/@someone) is a list of posts, not a
        # post, and would otherwise be queued as a download of everything they
        # ever published. Short links carry no path to check, and yt-dlp is
        # told elsewhere to refuse playlists, so they are let through.
        if host in {"vm.tiktok.com", "vt.tiktok.com"}:
            return "tiktok"
        segments = [segment for segment in path.split("/") if segment]
        if segments and (segments[0] == "t" or "video" in segments or "photo" in segments):
            return "tiktok"
        return None
    return None


def _extract_media_url(value: str) -> str:
    """Pull the link out of whatever was pasted.

    Share sheets paste "Judul lagu https://youtu.be/…", and links typed by hand
    or copied from some apps have no scheme. Only a token on a supported host
    is picked; anything else is returned as-is for validation to reject.
    """
    for token in value.split():
        token = token.strip("<>()[]\"'.,;")
        candidate = token if "://" in token else f"https://{token}"
        try:
            host = (urlparse(candidate).hostname or "").lower().rstrip(".")
        except ValueError:
            continue
        if host in YOUTUBE_HOSTS or host in TIKTOK_HOSTS:
            return candidate
    return value.strip()


def _youtube_video_id(host: str, path: str, query: str) -> str | None:
    """The single video a YouTube link points at, or None for any other page."""
    segments = [segment for segment in path.split("/") if segment]
    if host == "youtu.be":
        candidate = segments[0] if segments else ""
    elif segments == ["watch"]:
        candidate = (parse_qs(query).get("v") or [""])[0]
    elif len(segments) >= 2 and segments[0] in YOUTUBE_ID_PATHS:
        candidate = segments[1]
    else:
        return None
    return candidate if YOUTUBE_VIDEO_ID.fullmatch(candidate) else None


def _validate_media_url(value: str) -> tuple[str, str]:
    """Accept a single YouTube or TikTok link and say which one it is."""
    url = _extract_media_url(value)
    if not url or len(url) > 2048:
        raise HTTPException(422, "Masukkan URL YouTube atau TikTok yang valid.")
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(422, "URL tidak valid.") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or port not in {None, 80, 443}
    ):
        raise HTTPException(422, "URL tidak valid.")

    platform = _detect_platform(host, parsed.path or "")
    if platform is None:
        if host in TIKTOK_HOSTS:
            raise HTTPException(
                422,
                "Tempel link satu video TikTok, bukan link profil.",
            )
        raise HTTPException(422, "Hanya link video YouTube atau TikTok yang didukung.")
    if platform == "youtube":
        video_id = _youtube_video_id(host, parsed.path or "", parsed.query)
        if video_id is None:
            raise HTTPException(
                422,
                "Tempel link satu video YouTube, bukan playlist, channel, atau beranda.",
            )
        # Rebuilt from the ID so list=/start_radio= never reach yt-dlp: a Mix
        # link would otherwise be resolved as a playlist before the video.
        url = f"https://www.youtube.com/watch?v={video_id}"
    return url, platform


def _download_media_and_separate(
    job_id: str,
    url: str,
    platform: str,
    upload_job_dir: Path,
    result_job_dir: Path,
    quality_mode: Literal["balanced", "ultra"],
) -> None:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadCancelled

    source_path = upload_job_dir / "source.wav"
    label = PLATFORM_LABELS.get(platform, "URL")
    limit_minutes = MAX_TIKTOK_MINUTES if platform == "tiktok" else MAX_YOUTUBE_MINUTES

    def reject_long_video(info: dict[str, Any], *, incomplete: bool = False) -> None:
        if incomplete:
            return None
        duration = info.get("duration")
        if duration and duration > limit_minutes * 60:
            raise DownloadCancelled(
                f"Durasi video melebihi batas {limit_minutes} menit."
            )
        return None

    def progress_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        info = data.get("info_dict") or {}
        title = info.get("title")
        values: dict[str, Any] = {}
        if title:
            values["filename"] = f"{title}.wav"
        if status == "downloading":
            downloaded = data.get("downloaded_bytes") or 0
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            percent = int(downloaded / total * 100) if total else 0
            values.update(
                status="downloading",
                progress=max(3, min(22, 3 + int(percent * 0.19))),
                message=f"Mengunduh audio dari {label}…",
            )
        elif status == "finished":
            values.update(progress=23, message="Membuka audio ke WAV lossless…")
        if values:
            _update_job(job_id, **values)

    options: dict[str, Any] = {
        "format": "bestaudio/best",
        "outtmpl": str(upload_job_dir / "download.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",
        "max_filesize": MAX_UPLOAD_BYTES,
        "overwrites": True,
        "quiet": True,
        "no_warnings": True,
        # The progress bar goes to the job status, not the server console.
        "noprogress": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "js_runtimes": {"node": {}},
        "match_filter": reject_long_video,
        "progress_hooks": [progress_hook],
        # No audio postprocessor on purpose. YouTube and TikTok already serve
        # lossy audio; re-encoding it to MP3 would be a second generation of
        # loss for nothing, since every model downstream decodes to PCM anyway.
        # The stream is decoded once, below, and kept as it came out.
    }

    try:
        _update_job(
            job_id,
            status="downloading",
            progress=2,
            message=f"Membaca informasi video {label}…",
        )
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=True)
        if info and info.get("entries"):
            # A short link can still resolve to a list; take the first entry so
            # the title and duration below describe what was actually fetched.
            entries = [entry for entry in info["entries"] if entry]
            info = entries[0] if entries else None
        if not info:
            raise RuntimeError(f"Audio {label} gagal diambil.")
        downloaded = next(iter(sorted(upload_job_dir.glob("download.*"))), None)
        if downloaded is None:
            raise RuntimeError(f"Audio {label} gagal diambil.")

        _update_job(job_id, progress=24, message="Membuka audio ke WAV lossless…")
        _decode_to_wav(downloaded, source_path)
        _remove_intermediate(downloaded)

        title = info.get("title") or f"{label} audio"
        _update_job(
            job_id,
            filename=f"{title}.wav",
            progress=25,
            message="Audio siap. Memulai pemisahan…",
        )
    except Exception as exc:
        message = _friendly_download_error(exc, platform, label)
        _update_job(job_id, status="failed", progress=0, message=message[-1800:])
        return

    _run_separation(
        job_id,
        source_path,
        result_job_dir,
        start_progress=25,
        quality_mode=quality_mode,
    )
    # Lossless audio is bulky and useless once the stems exist, so it does not
    # sit in the upload folder until the cleanup job gets around to it.
    with jobs_lock:
        completed = jobs.get(job_id, {}).get("status") == "completed"
    if completed:
        _remove_intermediate(source_path)


def _decode_to_wav(source: Path, destination: Path) -> None:
    """Decode a downloaded stream to WAV without touching what it contains.

    float32, not 16-bit: a lossy decoder reconstructs peaks above full scale on
    a loud master, and 16-bit PCM would clip every one of them. Sample rate and
    channel count are left alone so nothing is resampled on the way through.
    """
    decoded = subprocess.run(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-vn",
            "-c:a",
            "pcm_f32le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        check=False,
    )
    if decoded.returncode != 0 or not destination.exists():
        raise RuntimeError(decoded.stderr.strip()[-1200:] or "Audio gagal dibuka ke WAV.")


def _friendly_download_error(exc: Exception, platform: str, label: str) -> str:
    """Turn yt-dlp's output into something a person can act on."""
    message = ANSI_CODES.sub("", str(exc)).strip()
    lowered = message.lower()
    # Order matters: a private video's error also mentions signing in, and the
    # useful half of that sentence is the "private" part.
    if "private video" in lowered or ("private" in lowered and platform == "tiktok"):
        return "Video bersifat privat dan tidak dapat diunduh."
    if "confirm you're not a bot" in lowered or "sign in" in lowered:
        return f"{label} meminta verifikasi. Coba video publik lain atau upload file langsung."
    if platform == "tiktok":
        if "no video formats" in lowered or "unable to extract" in lowered:
            return (
                "Post TikTok ini tidak punya audio yang bisa diambil. "
                "Post foto/slideshow belum didukung."
            )
        if "region" in lowered or "not available" in lowered:
            return "Video TikTok tidak tersedia di wilayah ini."
    if not message:
        return f"Gagal mengambil audio dari {label}."
    return message


def _loudnorm_filter(
    input_path: Path,
    prefilter: str,
    target_i: int,
    target_lra: int,
    creation_flags: int,
) -> str:
    """Build an accurate two-pass EBU R128 normalization filter."""
    base = f"loudnorm=I={target_i}:TP=-1.5:LRA={target_lra}"
    analysis = subprocess.run(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-nostats",
            "-i",
            str(input_path),
            "-af",
            f"{prefilter},{base}:print_format=json",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    if analysis.returncode != 0:
        raise RuntimeError(analysis.stderr[-1800:] or "Analisis loudness gagal.")

    matches = re.findall(r"\{\s*\"input_i\".*?\}", analysis.stderr, flags=re.DOTALL)
    if not matches:
        return f"{prefilter},{base}"

    measured = json.loads(matches[-1])
    keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if any(str(measured.get(key, "")).lower() in {"", "-inf", "inf", "nan"} for key in keys):
        return f"{prefilter},{base}"

    return (
        f"{prefilter},{base}"
        f":measured_I={measured['input_i']}"
        f":measured_TP={measured['input_tp']}"
        f":measured_LRA={measured['input_lra']}"
        f":measured_thresh={measured['input_thresh']}"
        f":offset={measured['target_offset']}"
        ":linear=true:print_format=summary"
    )


def _master_stem(
    input_path: Path,
    output_path: Path,
    stem: str,
    creation_flags: int,
) -> None:
    """Master a stem and write it losslessly.

    24-bit PCM: the limiter above already caps the signal at -1.5 dBTP so there
    is nothing to clip, and 24 bits puts the noise floor ~144 dB down, far below
    anything the separation itself leaves behind. It is also what a DAW expects
    when these stems get mixed again.
    """
    if stem == "vocals":
        # Conservative denoise avoids metallic artifacts on quiet vocal details.
        # dynaudnorm only engages above t: below it the gap between phrases is
        # left alone instead of being amplified up to m times, which is what
        # made the noise floor breathe between lines.
        # The de-esser is deliberately mild — separation masks sharpen sibilance,
        # but overdoing this turns an "s" into a lisp.
        prefilter = (
            "highpass=f=70:poles=2,"
            "afftdn=nr=8:nf=-52:tn=1,"
            "deesser=i=0.18:m=0.4:f=0.42,"
            "dynaudnorm=f=250:g=9:p=0.88:m=8:r=0.12:t=0.02:c=1,"
            "acompressor=threshold=0.125:ratio=3:attack=20:release=250:makeup=1.4:knee=2.828"
        )
        target_i, target_lra = -16, 7
    else:
        # Subsonic energy below the lowest musical note is rumble and DC drift
        # from the separation itself: cutting it frees headroom for the limiter.
        # Denoise stays light here because broadband reduction smears cymbals
        # and reverb long before it makes the hiss noticeably quieter.
        # Gentler compression keeps the instrumental transients natural. The
        # leveling window is unchanged; only the threshold moved, so a quiet
        # passage is no longer lifted as if it were the chorus.
        prefilter = (
            "highpass=f=28:poles=2,"
            "afftdn=nr=6:nf=-58:tn=1,"
            "dynaudnorm=f=400:g=7:p=0.9:m=4:r=0.1:t=0.02:c=1,"
            "acompressor=threshold=0.1778:ratio=1.8:attack=30:release=300:"
            "makeup=1.2:knee=2.828"
        )
        target_i, target_lra = -14, 9

    normalization = _loudnorm_filter(
        input_path,
        prefilter,
        target_i,
        target_lra,
        creation_flags,
    )
    filter_chain = (
        f"{normalization},"
        "alimiter=limit=0.841:attack=5:release=80:level=false:latency=true"
    )
    rendered = subprocess.run(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(input_path),
            "-af",
            filter_chain,
            "-ar",
            "44100",
            "-c:a",
            "pcm_s24le",
            "-map_metadata",
            "-1",
            str(output_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    if rendered.returncode != 0 or not output_path.exists():
        raise RuntimeError(rendered.stderr[-1800:] or f"Mastering {stem} gagal.")


def _encode_mp3(source: Path, destination: Path, creation_flags: int) -> None:
    """Make the listening copy from the mastered WAV.

    Encoding from the finished master rather than re-running the whole chain
    keeps the two files identical in everything but the codec, and skips a
    second loudness analysis pass.
    """
    encoded = subprocess.run(
        [
            FFMPEG_EXE,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-c:a",
            "libmp3lame",
            "-b:a",
            "320k",
            "-map_metadata",
            "-1",
            str(destination),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    if encoded.returncode != 0 or not destination.exists():
        raise RuntimeError(encoded.stderr[-1800:] or "Encode MP3 gagal.")


def _analyze_music(input_path: Path) -> dict[str, Any]:
    import librosa

    # Ten minutes is enough for stable metadata without making long mixes expensive.
    audio, sample_rate = librosa.load(input_path, sr=22050, mono=True, duration=600)
    if audio.size < sample_rate or float(np.max(np.abs(audio))) < 1e-5:
        raise ValueError("Audio terlalu pendek atau hening untuk dianalisis.")

    audio, _ = librosa.effects.trim(audio, top_db=45)
    harmonic, percussive = librosa.effects.hpss(audio, margin=(1.0, 3.0))

    tempo, beat_frames = librosa.beat.beat_track(
        y=percussive,
        sr=sample_rate,
        hop_length=512,
    )
    bpm = float(np.asarray(tempo).reshape(-1)[0])
    if not np.isfinite(bpm) or bpm <= 0:
        bpm = 0.0
    # Prefer the musically useful octave when the tracker returns half/double tempo.
    while 0 < bpm < 70:
        bpm *= 2
    while bpm > 190:
        bpm /= 2

    chroma = librosa.feature.chroma_cens(
        y=harmonic,
        sr=sample_rate,
        hop_length=512,
    )
    chroma_mean = np.mean(chroma, axis=1)
    chroma_mean /= np.linalg.norm(chroma_mean) + 1e-12

    major_profile = np.array(
        [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
    )
    minor_profile = np.array(
        [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
    )
    note_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    key_scores: list[tuple[float, int, str]] = []
    for tonic in range(12):
        key_scores.append(
            (float(np.corrcoef(chroma_mean, np.roll(major_profile, tonic))[0, 1]), tonic, "Major")
        )
        key_scores.append(
            (float(np.corrcoef(chroma_mean, np.roll(minor_profile, tonic))[0, 1]), tonic, "Minor")
        )
    key_scores.sort(reverse=True)
    best_score, tonic, mode = key_scores[0]
    second_score = key_scores[1][0]
    confidence = int(np.clip(55 + (best_score - second_score) * 180, 35, 95))
    key_chord = note_names[tonic] + ("m" if mode == "Minor" else "")

    # Match beat-synchronous chroma against 24 major/minor triad templates.
    if beat_frames.size >= 2:
        sync_chroma = librosa.util.sync(chroma, beat_frames, aggregate=np.median)
    else:
        sync_chroma = chroma
    templates: list[np.ndarray] = []
    chord_labels: list[str] = []
    for root in range(12):
        for is_minor in (False, True):
            template = np.zeros(12)
            template[[root, (root + (3 if is_minor else 4)) % 12, (root + 7) % 12]] = 1
            template /= np.linalg.norm(template)
            templates.append(template)
            chord_labels.append(note_names[root] + ("m" if is_minor else ""))
    chord_scores = np.stack(templates) @ sync_chroma
    winners = np.argmax(chord_scores, axis=0)
    counts = np.bincount(winners, minlength=len(chord_labels))
    dominant_chords = [
        chord_labels[index]
        for index in np.argsort(counts)[::-1]
        if counts[index] > 0
    ][:4]

    return {
        "bpm": int(round(bpm)) if bpm else None,
        "key": f"{note_names[tonic]} {mode}",
        "key_chord": key_chord,
        "key_confidence": confidence,
        "chords": dominant_chords,
    }


def _tail_gate(vocal_magnitude: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Where the voice has stopped but its room is still ringing.

    Built from the vocal's own decay: each bin keeps the loudest recent value,
    faded frame by frame, and is compared against what the voice is doing now.
    Returns that gate plus the raw vocal activity it was derived from, which
    tells the caller which frames have no vocal in them at all.
    """
    decay = 0.94
    envelope = np.empty_like(vocal_magnitude)
    accumulator = np.zeros(vocal_magnitude.shape[0], dtype=vocal_magnitude.dtype)
    for frame in range(vocal_magnitude.shape[1]):
        accumulator = np.maximum(accumulator * decay, vocal_magnitude[:, frame])
        # Left to decay through an instrumental section this underflows into
        # denormal floats, which are orders of magnitude slower to compute with
        # on x86 and are ~180 dB below anything audible anyway.
        accumulator[accumulator < 1e-9] = 0.0
        envelope[:, frame] = accumulator
    past = np.concatenate((np.zeros_like(envelope[:, :1]), envelope[:, :-1]), axis=1) * decay
    return past / (past + vocal_magnitude + 1e-8), past + vocal_magnitude


def _ghost_confidence(
    residual: np.ndarray,
    gate: np.ndarray,
    activity: np.ndarray,
) -> np.ndarray:
    """Per bin: how sure we are that there is an audible ghost to remove.

    Compares the band right after the voice stops against the same band when no
    vocal is ringing anywhere. A tail buried 20 dB under the music barely moves
    that ratio, and gets left alone — which is the point, because that is
    exactly where a mis-fitted filter would otherwise eat the music.
    """
    power = np.square(np.abs(residual))
    weight = gate.sum(axis=1)
    tail_power = (power * gate).sum(axis=1) / (weight + 1e-12)

    silent = activity < 0.02 * activity.max(axis=1, keepdims=True)
    counts = silent.sum(axis=1)
    floor_power = np.where(
        counts > 4,
        (power * silent).sum(axis=1) / np.maximum(counts, 1),
        np.inf,
    )

    prominence = tail_power / (floor_power + 1e-20)
    # Nothing below 1.5x power (1.8 dB), full strength from 4x (6 dB) up.
    confidence = np.clip((prominence - 1.5) / 2.5, 0.0, 1.0)
    return np.where(weight > 2, confidence, 0.0)


def _cancel_vocal_tail(
    residual_stft: np.ndarray,
    vocal_stft: np.ndarray,
) -> np.ndarray:
    """Subtract the vocal's reverb tail from the instrumental, coherently.

    A mask can only scale a bin, so where the tail and the music share a bin it
    can never remove one without the other. The tail is not independent noise
    though: it is the vocal played through the room, so for each frequency the
    room is one small filter over the previous frames. Fitting that filter by
    least squares and subtracting its output cancels the tail with the right
    phase, and leaves the music alone because music is uncorrelated with the
    vocal and so cannot be predicted from it.

    The first tap is one frame back, never zero, so the dry voice that was
    already subtracted is not subtracted a second time.
    """
    taps = max(INSTRUMENTAL_TAIL_TAPS, 1)
    n_bins, n_frames = residual_stft.shape
    if n_frames <= taps * 3:
        return residual_stft

    # Bins the voice never reaches have nothing to cancel. Skipping them is not
    # just faster: fitting a filter to near-silence produces denormal floats,
    # and denormal arithmetic is orders of magnitude slower on x86.
    energy = np.square(np.abs(vocal_stft)).sum(axis=1)
    if not energy.any():
        return residual_stft
    active = np.flatnonzero(energy > energy.max() * 1e-7)
    if active.size == 0:
        return residual_stft

    cleaned = residual_stft.copy()
    identity = np.eye(taps, dtype=np.complex64)
    for start in range(0, active.size, 256):
        bins = active[start : start + 256]
        vocal = vocal_stft[bins].astype(np.complex64)
        residual = residual_stft[bins].astype(np.complex64)

        history = np.zeros((bins.size, taps, n_frames), dtype=np.complex64)
        for tap in range(taps):
            history[:, tap, tap + 1 :] = vocal[:, : n_frames - tap - 1]

        # Every second frame is enough to estimate a filter this short, and it
        # halves the cost of the two einsums that dominate the runtime.
        sampled_history = history[:, :, ::2]
        sampled_residual = residual[:, ::2]
        correlation = np.einsum("fkt,flt->fkl", sampled_history, sampled_history.conj())
        cross = np.einsum("fkt,ft->fk", sampled_history, sampled_residual.conj())

        trace = np.trace(correlation, axis1=1, axis2=2).real
        ridge = (INSTRUMENTAL_TAIL_RIDGE * trace / taps + 1e-6)[:, None, None] * identity
        try:
            filters = np.linalg.solve(correlation + ridge, cross[..., None])[..., 0]
        except np.linalg.LinAlgError:
            continue
        prediction = np.einsum("fk,fkt->ft", filters.conj(), history)

        # Only subtract where a tail can be: the voice rang recently but is not
        # sounding now. While it sings, the music under it is masked anyway,
        # and leaving those frames alone removes most of the opportunity to
        # mistake instrument bleed in the vocal stem for reverb.
        gate, activity = _tail_gate(np.abs(vocal))
        prediction *= gate

        # Least-squares fitted against a target dominated by music estimates a
        # noisy filter, and subtracting a noisy prediction *adds* energy. So
        # the prediction is scaled by its own projection onto the residual,
        # clipped to [0, 1]: at 0 nothing happens, at 1 it is the best possible
        # subtraction, and energy can never go up.
        denominator = np.square(np.abs(prediction)).sum(axis=1)
        numerator = np.real(residual * prediction.conj()).sum(axis=1)
        scale = np.clip(numerator / (denominator + 1e-20), 0.0, 1.0)

        # Energy going down is not the same as the result being better: a vocal
        # stem carries some instrument bleed, and a filter can use it to
        # "predict" music and subtract that instead. The protection is to act
        # only where a ghost is measurably there — where the band, in the
        # frames after the voice stops, is louder than that same band is when
        # no vocal is ringing at all. When the tail is buried under the music
        # this is near 1, nothing is subtracted, and nothing can be damaged.
        cleaned[bins] = residual - (
            INSTRUMENTAL_TAIL_STRENGTH
            * (scale * _ghost_confidence(residual, gate, activity))[:, None]
            * prediction
        )
    return cleaned


def _clean_instrumental_block(
    mixture: np.ndarray,
    primary_vocal: np.ndarray,
    extra_vocals: list[np.ndarray],
) -> np.ndarray:
    """Build the instrumental: exact subtraction first, then clean the leftovers.

    `primary_vocal` is removed in the time domain, so everything the model got
    right disappears with the correct phase. `extra_vocals` are second opinions
    used only to duck what that subtraction left behind, and the reverb tail is
    handled separately because no model labels it vocal in the first place.
    """
    import librosa

    length, channels = mixture.shape
    residual = mixture - primary_vocal
    if INSTRUMENTAL_TAIL_STRENGTH <= 0 and not extra_vocals:
        return residual

    output = np.zeros_like(mixture)
    for channel in range(channels):
        residual_stft = librosa.stft(
            residual[:, channel], n_fft=STFT_N_FFT, hop_length=STFT_HOP
        )
        vocal_stft = librosa.stft(
            primary_vocal[:, channel], n_fft=STFT_N_FFT, hop_length=STFT_HOP
        )
        vocal_magnitude = np.abs(vocal_stft)

        if INSTRUMENTAL_TAIL_STRENGTH > 0:
            residual_stft = _cancel_vocal_tail(residual_stft, vocal_stft)

        residual_magnitude = np.abs(residual_stft)
        gain = np.ones_like(residual_magnitude)
        if extra_vocals:
            # Only the magnitude the other models claim *beyond* what was already
            # subtracted counts as bleed, and the duck is floored so a
            # disagreement can never punch a hole in the music.
            claimed = vocal_magnitude
            for estimate in extra_vocals:
                claimed = np.maximum(
                    claimed,
                    np.abs(
                        librosa.stft(
                            estimate[:, channel], n_fft=STFT_N_FFT, hop_length=STFT_HOP
                        )
                    ),
                )
            bleed = np.clip(claimed - vocal_magnitude, 0.0, None)
            gain *= np.clip(
                1.0 - bleed / (residual_magnitude + 1e-8),
                INSTRUMENTAL_BLEED_FLOOR,
                1.0,
            )

        output[:, channel] = librosa.istft(
            residual_stft * gain, hop_length=STFT_HOP, length=length
        )
    return output


def _refine_vocals(
    input_path: Path,
    demucs_vocals: Path,
    demucs_instrumental: Path,
    result_root: Path,
    creation_flags: int,
) -> tuple[Path, Path]:
    import librosa
    import soundfile as sf
    from scipy.ndimage import gaussian_filter

    refinement_dir = result_root / "kim_refinement"
    refinement_dir.mkdir(parents=True, exist_ok=True)
    kim_vocals = refinement_dir / "kim_vocals.wav"
    command = [
        str(AUDIO_SEPARATOR_CLI),
        str(input_path),
        "--model_file_dir",
        str(MODEL_DIR),
        "--model_filename",
        REFINEMENT_MODEL,
        "--output_dir",
        str(refinement_dir),
        "--output_format",
        "WAV",
        "--single_stem",
        "Vocals",
        "--custom_output_names",
        json.dumps({"Vocals": "kim_vocals"}),
    ]
    process = subprocess.run(
        command,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    if process.returncode != 0 or not kim_vocals.exists():
        details = (process.stdout + "\n" + process.stderr).strip()
        raise RuntimeError(details[-1800:] or "Model cross-check vocal gagal.")

    refined_vocals = demucs_vocals.with_name("vocals_refined.wav")
    refined_instrumental = demucs_instrumental.with_name("instrumental_refined.wav")
    with (
        sf.SoundFile(demucs_vocals) as vocal_file,
        sf.SoundFile(demucs_instrumental) as instrumental_file,
        sf.SoundFile(kim_vocals) as kim_file,
    ):
        if not (
            vocal_file.samplerate == instrumental_file.samplerate == kim_file.samplerate
            and vocal_file.channels == instrumental_file.channels == kim_file.channels
        ):
            raise RuntimeError("Format output model vocal tidak cocok untuk hybrid refinement.")

        sample_rate = vocal_file.samplerate
        channels = vocal_file.channels
        block_size = sample_rate * 60
        # Each block is transformed on its own, so without shared context the
        # STFT windows at a block edge see silence and leave an audible seam
        # every 60 seconds. Carrying one window of the previous block across
        # the boundary and trimming it back off removes that seam.
        context = STFT_N_FFT
        carry_demucs = np.zeros((0, channels), dtype=np.float32)
        carry_kim = np.zeros((0, channels), dtype=np.float32)
        carry_mixture = np.zeros((0, channels), dtype=np.float32)
        with (
            sf.SoundFile(
                refined_vocals,
                mode="w",
                samplerate=sample_rate,
                channels=channels,
                subtype="FLOAT",
            ) as vocal_output,
            sf.SoundFile(
                refined_instrumental,
                mode="w",
                samplerate=sample_rate,
                channels=channels,
                subtype="FLOAT",
            ) as instrumental_output,
        ):
            while True:
                demucs_block = vocal_file.read(block_size, dtype="float32", always_2d=True)
                if not len(demucs_block):
                    break
                instrumental_block = instrumental_file.read(
                    len(demucs_block), dtype="float32", always_2d=True
                )
                kim_block = kim_file.read(len(demucs_block), dtype="float32", always_2d=True)
                if len(instrumental_block) < len(demucs_block):
                    instrumental_block = np.pad(
                        instrumental_block,
                        ((0, len(demucs_block) - len(instrumental_block)), (0, 0)),
                    )
                if len(kim_block) < len(demucs_block):
                    kim_block = np.pad(
                        kim_block,
                        ((0, len(demucs_block) - len(kim_block)), (0, 0)),
                    )
                # Demucs ran with --other-method minus, so the two stems still
                # add back up to the untouched mixture.
                mixture_block = instrumental_block + demucs_block

                demucs_padded = np.concatenate((carry_demucs, demucs_block))
                kim_padded = np.concatenate((carry_kim, kim_block))
                mixture_padded = np.concatenate((carry_mixture, mixture_block))
                offset = len(carry_demucs)

                refined_padded = np.zeros_like(demucs_padded)
                for channel in range(channels):
                    demucs_stft = librosa.stft(
                        demucs_padded[:, channel], n_fft=STFT_N_FFT, hop_length=STFT_HOP
                    )
                    kim_stft = librosa.stft(
                        kim_padded[:, channel], n_fft=STFT_N_FFT, hop_length=STFT_HOP
                    )
                    ratio = np.abs(kim_stft) / (np.abs(demucs_stft) + 1e-7)
                    # Conservative consensus mask for the exported vocal: where
                    # the two models disagree the bin is attenuated, never cut,
                    # so quiet vocal detail survives.
                    mask = np.sqrt(np.clip(ratio, VOCAL_MASK_FLOOR, 1.0))
                    mask = gaussian_filter(mask, sigma=(1.0, 1.0))
                    refined_padded[:, channel] = librosa.istft(
                        demucs_stft * mask,
                        hop_length=STFT_HOP,
                        length=len(demucs_padded),
                    )

                # Subtracting the refined vocal keeps the two stems adding back
                # up to the mixture, which matters: where Kim disagrees with
                # Demucs the energy is usually an instrument Demucs mislabelled,
                # and it belongs in the music. The opposite case — Kim hearing
                # vocal that Demucs missed — is what the second estimate is
                # passed in for, and the reverb tail is handled separately
                # because no model calls a tail vocal at all.
                instrumental_padded = _clean_instrumental_block(
                    mixture_padded,
                    refined_padded,
                    [kim_padded],
                )

                vocal_output.write(refined_padded[offset:])
                instrumental_output.write(instrumental_padded[offset:])

                carry_demucs = demucs_padded[-context:].copy()
                carry_kim = kim_padded[-context:].copy()
                carry_mixture = mixture_padded[-context:].copy()

    _remove_intermediate(kim_vocals)
    return refined_vocals, refined_instrumental


def _separate_ultra_vocal_clean(
    job_id: str,
    input_path: Path,
    result_root: Path,
    creation_flags: int,
) -> tuple[Path, Path]:
    """Run the bleedless RoFormer ensemble and rebuild music from the mixture."""
    import soundfile as sf

    ultra_dir = result_root / "ultra_vocal_clean"
    ultra_dir.mkdir(parents=True, exist_ok=True)
    source_wav = ultra_dir / "source_float.wav"
    ultra_vocals = ultra_dir / "vocals_ultra.wav"
    ultra_instrumental = ultra_dir / "instrumental_ultra.wav"

    _update_job(
        job_id,
        status="processing",
        progress=27,
        message="Menyiapkan audio lossless untuk mode Ultra…",
    )
    prepare = subprocess.run(
        [
            FFMPEG_EXE,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(input_path),
            "-vn",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-c:a",
            "pcm_f32le",
            str(source_wav),
        ],
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creation_flags,
        check=False,
    )
    if prepare.returncode != 0 or not source_wav.exists():
        raise RuntimeError(prepare.stderr.strip() or "Audio gagal disiapkan untuk mode Ultra.")

    _update_job(
        job_id,
        status="refining",
        progress=30,
        message="Menyiapkan dua model RoFormer bleedless… Unduhan pertama dapat lama.",
    )
    command = [
        str(AUDIO_SEPARATOR_CLI),
        str(source_wav),
        "--model_file_dir",
        str(MODEL_DIR),
        "--ensemble_preset",
        ULTRA_ENSEMBLE_PRESET,
        "--output_dir",
        str(ultra_dir),
        "--output_format",
        "WAV",
        "--single_stem",
        "Vocals",
        "--normalization",
        "1.0",
        "--custom_output_names",
        json.dumps({"Vocals": "vocals_ultra"}),
    ]

    output_lines: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=BASE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=creation_flags,
    )
    assert process.stdout is not None
    ultra_stage = 0
    for line in process.stdout:
        line = line.strip()
        if not line:
            continue
        output_lines.append(line)
        output_lines = output_lines[-60:]
        lower_line = line.lower()
        if "revive_v2" in lower_line and "processing with model" in lower_line:
            ultra_stage = max(ultra_stage, 1)
            _update_job(
                job_id,
                progress=36,
                message="Ultra 1/2: menyaring vokal dengan BS-RoFormer Revive V2…",
            )
        elif "ft2_bleedless" in lower_line and "processing with model" in lower_line:
            ultra_stage = max(ultra_stage, 2)
            _update_job(
                job_id,
                progress=59,
                message="Ultra 2/2: memvalidasi vokal dengan Kim FT2 Bleedless…",
            )
        elif "ensembling" in lower_line and ultra_stage >= 2:
            _update_job(
                job_id,
                progress=82,
                message="Menggabungkan hasil dengan filter min-FFT anti-bleed…",
            )

    return_code = process.wait()
    if return_code != 0 or not ultra_vocals.exists():
        details = "\n".join(output_lines)
        raise RuntimeError(details[-3000:] or "Ensemble RoFormer Ultra gagal menghasilkan vokal.")

    _update_job(
        job_id,
        progress=87,
        message="Mengembalikan bagian non-vokal ke instrumental…",
    )
    with sf.SoundFile(source_wav) as mixture_file, sf.SoundFile(ultra_vocals) as vocal_file:
        sample_rate = mixture_file.samplerate
        channels = mixture_file.channels
        if vocal_file.samplerate != sample_rate:
            raise RuntimeError("Sample rate hasil Ultra tidak cocok dengan sumber audio.")

        block_size = sample_rate * 60
        context = STFT_N_FFT
        carry_mixture = np.zeros((0, channels), dtype=np.float32)
        carry_vocal = np.zeros((0, channels), dtype=np.float32)
        with sf.SoundFile(
            ultra_instrumental,
            mode="w",
            samplerate=sample_rate,
            channels=channels,
            subtype="FLOAT",
        ) as instrumental_output:
            while True:
                mixture_block = mixture_file.read(block_size, dtype="float32", always_2d=True)
                if not len(mixture_block):
                    break
                vocal_block = vocal_file.read(len(mixture_block), dtype="float32", always_2d=True)
                if vocal_block.shape[1] == 1 and channels == 2:
                    vocal_block = np.repeat(vocal_block, 2, axis=1)
                if vocal_block.shape[1] != channels:
                    raise RuntimeError("Jumlah channel hasil Ultra tidak cocok dengan sumber audio.")
                if len(vocal_block) < len(mixture_block):
                    vocal_block = np.pad(
                        vocal_block,
                        ((0, len(mixture_block) - len(vocal_block)), (0, 0)),
                    )
                vocal_block = vocal_block[: len(mixture_block)]

                mixture_padded = np.concatenate((carry_mixture, mixture_block))
                vocal_padded = np.concatenate((carry_vocal, vocal_block))
                offset = len(carry_mixture)

                # Ultra's ensemble is already bleedless on the vocal side, but
                # a plain subtraction still leaves the reverb tail in the music
                # because no model labels a tail as vocal.
                instrumental_padded = _clean_instrumental_block(
                    mixture_padded,
                    vocal_padded,
                    [],
                )
                instrumental_output.write(instrumental_padded[offset:])

                carry_mixture = mixture_padded[-context:].copy()
                carry_vocal = vocal_padded[-context:].copy()

    _remove_intermediate(source_wav)
    return ultra_vocals, ultra_instrumental


def _run_separation(
    job_id: str,
    input_path: Path,
    result_root: Path,
    start_progress: int = 5,
    quality_mode: Literal["balanced", "ultra"] = "balanced",
) -> None:
    creation_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        if quality_mode == "ultra":
            _update_job(
                job_id,
                status="processing",
                progress=start_progress,
                message="Menyiapkan ensemble RoFormer Ultra Human Focus…",
            )
            raw_vocals, raw_instrumental = _separate_ultra_vocal_clean(
                job_id,
                input_path,
                result_root,
                creation_flags,
            )
            refinement_applied = True
            separation_label = ULTRA_MODEL_LABEL
        else:
            _update_job(
                job_id,
                status="processing",
                progress=start_progress,
                message="Menyiapkan model spesialis vokal… Proses pertama dapat mengunduh model.",
            )
            command = [
                sys.executable,
                "-m",
                "demucs",
                "--name",
                MODEL_NAME,
                "--two-stems",
                "vocals",
                "--other-method",
                "minus",
                "--device",
                "cpu",
                "--out",
                str(result_root),
                "--filename",
                "{stem}.{ext}",
                str(input_path),
            ]
            output_lines: list[str] = []
            separation_started = False
            process = subprocess.Popen(
                command,
                cwd=BASE_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
            )
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                output_lines.append(line)
                output_lines = output_lines[-30:]
                if "Separating track" in line:
                    separation_started = True
                match = re.search(r"(\d{1,3})%", line)
                if match and separation_started:
                    percent = min(int(match.group(1)), 100)
                    _update_job(
                        job_id,
                        progress=max(
                            start_progress,
                            min(80, start_progress + int(percent * (80 - start_progress) / 100)),
                        ),
                        message="Memisahkan vokal dan instrumen…",
                    )

            return_code = process.wait()
            if return_code != 0:
                details = "\n".join(output_lines)
                raise RuntimeError(details or f"Demucs berhenti dengan kode {return_code}")

            model_dir = result_root / MODEL_NAME.replace("hf://", "").replace("/", "_")
            raw_vocals = model_dir / "vocals.wav"
            raw_instrumental = model_dir / "minus_vocals.wav"
            if not raw_vocals.exists() or not raw_instrumental.exists():
                raise RuntimeError("Demucs selesai, tetapi berkas hasil tidak ditemukan.")

            refinement_applied = False
            separation_label = MODEL_NAME
            _update_job(
                job_id,
                status="refining",
                progress=82,
                message="Cross-check suara manusia dan menekan kebocoran melodi…",
            )
            try:
                refined_vocals, refined_instrumental = _refine_vocals(
                    input_path,
                    raw_vocals,
                    raw_instrumental,
                    result_root,
                    creation_flags,
                )
                _remove_intermediate(raw_vocals)
                _remove_intermediate(raw_instrumental)
                raw_vocals = refined_vocals
                raw_instrumental = refined_instrumental
                refinement_applied = True
                separation_label = MODEL_LABEL
            except Exception:
                # Keep the reliable Demucs output if the cross-check model cannot run.
                logger.warning(
                    "Vocal refinement failed for job %s; using Demucs stems.",
                    job_id,
                    exc_info=True,
                )

        _update_job(
            job_id,
            status="analyzing",
            progress=91,
            message="Mendeteksi BPM, key, dan chord dominan…",
        )
        try:
            analysis = _analyze_music(raw_instrumental)
        except Exception:
            logger.warning("Music analysis failed for job %s.", job_id, exc_info=True)
            analysis = {
                "bpm": None,
                "key": "Tidak terdeteksi",
                "key_chord": "—",
                "key_confidence": 0,
                "chords": [],
            }

        # Demucs fallback keeps vocals.wav as an input. Separate final files
        # so FFmpeg never writes over it and raw cleanup cannot delete a master.
        mastered_dir = result_root / "mastered"
        mastered_dir.mkdir(parents=True, exist_ok=True)
        vocals = mastered_dir / "vocals.wav"
        instrumental = mastered_dir / "no_vocals.wav"
        vocals_mp3 = mastered_dir / "vocals.mp3"
        instrumental_mp3 = mastered_dir / "no_vocals.mp3"
        _update_job(
            job_id,
            status="mastering",
            progress=92,
            message="Membersihkan noise dan menstabilkan volume vokal…",
        )
        _master_stem(raw_vocals, vocals, "vocals", creation_flags)
        _update_job(
            job_id,
            progress=95,
            message="Menormalkan instrumental dan memasang limiter…",
        )
        _master_stem(raw_instrumental, instrumental, "instrumental", creation_flags)

        _update_job(
            job_id,
            progress=98,
            message="Menyiapkan salinan MP3 untuk didengarkan…",
        )
        mp3_outputs: dict[str, Path | None] = {}
        warnings: list[str] = []
        for stem, source, destination in (
            ("vocals", vocals, vocals_mp3),
            ("instrumental", instrumental, instrumental_mp3),
        ):
            try:
                _encode_mp3(source, destination, creation_flags)
                mp3_outputs[stem] = destination
            except Exception:
                logger.warning(
                    "MP3 export failed for job %s (%s); keeping the WAV.",
                    job_id,
                    stem,
                    exc_info=True,
                )
                _remove_intermediate(destination)
                mp3_outputs[stem] = None
                label = "vokal" if stem == "vocals" else "instrumental"
                warnings.append(f"Salinan MP3 {label} gagal dibuat. Hasil WAV tetap siap diunduh.")

        _remove_intermediate(raw_vocals)
        _remove_intermediate(raw_instrumental)

        _update_job(
            job_id,
            status="completed",
            progress=100,
            message=(
                "Selesai! Hasil WAV siap; sebagian salinan MP3 tidak tersedia."
                if warnings
                else "Selesai! Noise dibersihkan dan volume sudah distabilkan."
            ),
            vocals=vocals,
            instrumental=instrumental,
            vocals_mp3=mp3_outputs["vocals"],
            instrumental_mp3=mp3_outputs["instrumental"],
            warnings=warnings,
            analysis=analysis,
            refinement_applied=refinement_applied,
            quality_mode=quality_mode,
            separation_label=separation_label,
        )
        _save_job_record(job_id, result_root)
    except Exception as exc:
        logger.exception("Audio separation failed for job %s.", job_id)
        message = str(exc).strip()
        if (
            isinstance(exc, FileNotFoundError)
            and exc.filename
            and Path(exc.filename).stem.lower() in {"ffmpeg", "ffprobe"}
        ):
            message = "FFmpeg belum tersedia. Jalankan .\\server.ps1 -Setup lalu coba lagi."
        elif not message:
            message = "Terjadi kesalahan saat menjalankan Demucs."
        _update_job(job_id, status="failed", progress=0, message=message[-1800:])


@app.on_event("startup")
def startup() -> None:
    _cleanup_old_files()


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ffmpeg": FFMPEG_AVAILABLE,
        "model": MODEL_NAME,
        "model_label": MODEL_LABEL,
        "refinement_model": REFINEMENT_MODEL,
        "ultra_model_label": ULTRA_MODEL_LABEL,
        "quality_modes": ["balanced", "ultra"],
        "device": "cpu",
        "max_upload_mb": MAX_UPLOAD_MB,
        "max_youtube_minutes": MAX_YOUTUBE_MINUTES,
        "max_tiktok_minutes": MAX_TIKTOK_MINUTES,
        "url_sources": {
            "youtube": {"label": "YouTube", "max_minutes": MAX_YOUTUBE_MINUTES},
            "tiktok": {"label": "TikTok", "max_minutes": MAX_TIKTOK_MINUTES},
        },
    }


@app.post("/api/jobs", status_code=202)
async def create_job(
    file: UploadFile = File(...),
    quality_mode: Literal["balanced", "ultra"] = Form("balanced"),
) -> dict[str, Any]:
    if not FFMPEG_AVAILABLE:
        raise HTTPException(503, "FFmpeg belum ditemukan. Jalankan .\\server.ps1 -Setup terlebih dahulu.")

    original_name = Path(file.filename or "audio").name
    extension = Path(original_name).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        supported = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise HTTPException(415, f"Format tidak didukung. Gunakan: {supported}")

    job_id = uuid.uuid4().hex
    upload_job_dir = UPLOAD_DIR / job_id
    result_job_dir = RESULT_DIR / job_id
    upload_job_dir.mkdir(parents=True)
    result_job_dir.mkdir(parents=True)
    input_path = upload_job_dir / f"input{extension}"

    size = 0
    try:
        with input_path.open("wb") as destination:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise HTTPException(413, f"Ukuran maksimum adalah {MAX_UPLOAD_MB} MB.")
                destination.write(chunk)
    except Exception:
        shutil.rmtree(upload_job_dir, ignore_errors=True)
        shutil.rmtree(result_job_dir, ignore_errors=True)
        raise
    finally:
        await file.close()

    if size == 0:
        shutil.rmtree(upload_job_dir, ignore_errors=True)
        shutil.rmtree(result_job_dir, ignore_errors=True)
        raise HTTPException(400, "Berkas audio kosong.")

    job = {
        "id": job_id,
        "status": "queued",
        "progress": 1,
        "message": "Masuk antrean pemrosesan…",
        "filename": original_name,
        "quality_mode": quality_mode,
        "created_at": time.time(),
    }
    with jobs_lock:
        jobs[job_id] = job

    executor.submit(
        _run_separation,
        job_id,
        input_path,
        result_job_dir,
        5,
        quality_mode,
    )
    return _public_job(job)


@app.post("/api/media", status_code=202)
def create_media_job(request: MediaUrlRequest) -> dict[str, Any]:
    """Queue a job from a pasted link, whichever supported site it points at."""
    if not FFMPEG_AVAILABLE:
        raise HTTPException(503, "FFmpeg belum ditemukan. Jalankan .\\server.ps1 -Setup terlebih dahulu.")

    url, platform = _validate_media_url(request.url)
    label = PLATFORM_LABELS[platform]
    job_id = uuid.uuid4().hex
    upload_job_dir = UPLOAD_DIR / job_id
    result_job_dir = RESULT_DIR / job_id
    upload_job_dir.mkdir(parents=True)
    result_job_dir.mkdir(parents=True)

    job = {
        "id": job_id,
        "status": "queued",
        "progress": 1,
        "message": f"Link {label} masuk antrean pemrosesan…",
        "filename": f"{label} audio",
        "quality_mode": request.quality_mode,
        "source": platform,
        "created_at": time.time(),
    }
    with jobs_lock:
        jobs[job_id] = job

    executor.submit(
        _download_media_and_separate,
        job_id,
        url,
        platform,
        upload_job_dir,
        result_job_dir,
        request.quality_mode,
    )
    return _public_job(job)


@app.post("/api/youtube", status_code=202, include_in_schema=False)
def create_youtube_job(request: MediaUrlRequest) -> dict[str, Any]:
    """Kept so older bookmarks and scripts calling this path keep working."""
    return create_media_job(request)


@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """Send API root to UI."""
    return RedirectResponse(FRONTEND_URL)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = _find_job(job_id)
    if job is None:
        if JOB_ID_PATTERN.fullmatch(job_id) and (RESULT_DIR / job_id).is_dir():
            raise HTTPException(
                404,
                "Proses terhenti karena server backend dimulai ulang. Pisahkan ulang lagunya.",
            )
        raise HTTPException(404, "Job tidak ditemukan atau sudah kedaluwarsa.")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/files/{stem}")
def get_result(
    job_id: str,
    stem: str,
    audio_format: str = Query("mp3", alias="format"),
) -> FileResponse:
    if stem not in {"vocals", "instrumental"}:
        raise HTTPException(404, "Hasil tidak ditemukan.")
    if audio_format not in {"mp3", "wav"}:
        raise HTTPException(422, "Format hasil harus mp3 atau wav.")

    job = _find_job(job_id)
    if job is None or job["status"] != "completed":
        raise HTTPException(404, "Hasil belum tersedia.")
    path = job.get(_stem_key(stem, audio_format))
    original_name = job["filename"]

    if not path or not Path(path).exists():
        raise HTTPException(404, "Hasil belum tersedia.")

    return FileResponse(
        path,
        media_type="audio/wav" if audio_format == "wav" else "audio/mpeg",
        filename=_safe_download_name(original_name, stem, audio_format),
    )



