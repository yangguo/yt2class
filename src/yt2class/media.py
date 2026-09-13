"""Safe adapters for yt-dlp and ffmpeg."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Sequence


class MediaError(RuntimeError):
    """Raised when an external media command cannot produce its artifact."""


def build_title_command(url: str) -> list[str]:
    """Build a single-video metadata lookup for a learner-facing title."""

    return [
        "yt-dlp",
        "--no-warnings",
        "--skip-download",
        "--no-playlist",
        "--print",
        "%(title)s",
        "--",
        url,
    ]


def build_download_command(url: str, media_dir: Path) -> list[str]:
    """Build a non-shell yt-dlp command for video and usable captions."""

    return [
        "yt-dlp",
        "--no-playlist",
        "--format",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/b",
        "--merge-output-format",
        "mp4",
        "--write-subs",
        "--write-auto-subs",
        "--sub-langs",
        "ja,zh-Hans,zh-Hant",
        "--convert-subs",
        "vtt",
        "--output",
        str(media_dir / "source.%(ext)s"),
        "--",
        url,
    ]


def build_frame_command(video_path: Path, timestamp: float, output_path: Path) -> list[str]:
    """Build an ffmpeg command that extracts one source-faithful JPEG frame."""

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{timestamp:.3f}",
        "-i",
        str(video_path),
        "-frames:v",
        "1",
        "-vf",
        "scale=1280:-2",
        "-q:v",
        "2",
        str(output_path),
    ]


def run_media_command(command: Sequence[str]) -> None:
    """Run an external command without a shell and surface a compact failure."""

    try:
        subprocess.run(
            list(command),
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise MediaError(f"Required executable is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown command failure").strip()
        raise MediaError(f"{command[0]} failed: {detail}") from error


def fetch_video_title(url: str) -> str:
    """Fetch one video title without downloading the playlist."""

    command = build_title_command(url)
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise MediaError(f"Required executable is unavailable: {command[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "unknown metadata failure").strip()
        raise MediaError(f"{command[0]} failed: {detail}") from error
    title = next((line.strip() for line in result.stdout.splitlines() if line.strip()), "")
    if not title:
        raise MediaError("yt-dlp returned an empty video title")
    return title


def download_lesson(url: str, media_dir: Path) -> Path:
    """Download one lesson and return its normalized MP4 path."""

    media_dir.mkdir(parents=True, exist_ok=True)
    run_media_command(build_download_command(url, media_dir))
    source_video = media_dir / "source.mp4"
    if source_video.exists() and source_video.stat().st_size > 0:
        return source_video

    candidates = sorted(media_dir.glob("source*.mp4"), key=lambda path: path.stat().st_size)
    if not candidates:
        raise MediaError("yt-dlp completed but did not produce an MP4 source video")
    candidates[-1].replace(source_video)
    return source_video


def extract_frame(video_path: Path, timestamp: float, output_path: Path) -> Path:
    """Extract an original frame and verify that ffmpeg wrote a non-empty file."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    run_media_command(build_frame_command(video_path, timestamp, output_path))
    if not output_path.exists() or output_path.stat().st_size == 0:
        raise MediaError(f"ffmpeg did not produce a frame at {timestamp:.3f}s")
    return output_path
