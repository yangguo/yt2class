"""Controlled yt-dlp adapter for source media ingestion."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from threading import Event
from typing import Callable

from yt2class.adapters.process import (
    ProcessCancelled,
    ProcessError,
    ProcessTimedOut,
    ProcessUnavailable,
    run_process,
)
from yt2class.domain.source import SourceInput


class YtDlpError(RuntimeError):
    """Raised when yt-dlp cannot produce a usable media artifact."""


class YtDlpCancelled(YtDlpError):
    """Raised when a caller cancels a download."""


class YtDlpTimeout(YtDlpError):
    """Raised when yt-dlp exceeds its subprocess timeout."""


@dataclass(frozen=True)
class YtDlpDownload:
    """The actual media artifact and command output returned by yt-dlp."""

    media_path: Path
    stdout: str = ""
    stderr: str = ""
    info_path: Path | None = None


Runner = Callable[..., object]


def build_download_command(url: str, output_dir: Path) -> list[str]:
    """Build a shell-free single-video command with an inspectable output path.

    The output template deliberately keeps the title and extension supplied by
    yt-dlp.  ``after_move:filepath`` is parsed by :func:`download_video`, so
    callers never need to guess whether a download was WebM, MP4, or MKV.
    """

    output_template = Path(output_dir) / "%(title)s [%(id)s].%(ext)s"
    return [
        "yt-dlp",
        "--no-playlist",
        "--format",
        "bv*+ba/b",
        "--write-info-json",
        "--print",
        "after_move:filepath",
        "--output",
        str(output_template),
        "--",
        str(url),
    ]


def _coerce_youtube(source: SourceInput | str) -> SourceInput:
    if isinstance(source, SourceInput):
        result = source
    else:
        result = SourceInput.from_value(source)
    if result.kind != "youtube":
        raise YtDlpError("yt-dlp adapter accepts YouTube sources only")
    return result


def _file_signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _snapshot_media(output_dir: Path) -> dict[Path, tuple[int, int]]:
    snapshot: dict[Path, tuple[int, int]] = {}
    for path in output_dir.rglob("*"):
        if not path.is_file():
            continue
        is_media = path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
        is_info = path.name.endswith(".info.json")
        if not is_media and not is_info:
            continue
        resolved = path.resolve(strict=False)
        signature = _file_signature(resolved)
        if signature is not None:
            snapshot[resolved] = signature
    return snapshot


def _is_new_or_changed(path: Path, before: dict[Path, tuple[int, int]]) -> bool:
    signature = _file_signature(path)
    return signature is not None and signature != before.get(path.resolve(strict=False))


def _printed_media_path(
    stdout: str,
    output_dir: Path,
    before: dict[Path, tuple[int, int]],
) -> Path | None:
    """Find the actual post-processed path printed by yt-dlp."""

    candidates: list[Path] = []
    for line in stdout.splitlines():
        value = line.strip()
        if not value or value.endswith(".part"):
            continue
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = output_dir / candidate
        try:
            candidate = candidate.resolve(strict=True)
        except OSError:
            continue
        if (
            candidate.is_relative_to(output_dir)
            and candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            and candidate.is_file()
            and candidate.stat().st_size > 0
            and _is_new_or_changed(candidate, before)
        ):
            candidates.append(candidate)
    if not candidates:
        return None
    return candidates[-1]


def _discover_media_path(
    output_dir: Path,
    before: dict[Path, tuple[int, int]],
) -> Path | None:
    candidates: list[Path] = []
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.name.endswith((".part", ".ytdl")):
            continue
        if path.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov"}:
            continue
        resolved = path.resolve(strict=False)
        if not resolved.is_relative_to(output_dir):
            continue
        if _is_new_or_changed(resolved, before):
            candidates.append(resolved)
    if len(candidates) != 1:
        return None
    return candidates[0].resolve()


def _locate_info_path(
    media_path: Path,
    output_dir: Path,
    before: dict[Path, tuple[int, int]],
    expected_video_id: str,
) -> Path:
    candidates = [
        media_path.with_suffix(".info.json"),
        Path(f"{media_path}.info.json"),
    ]
    candidates.extend(sorted(output_dir.glob(f"{media_path.stem}*.info.json")))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(output_dir):
            continue
        if not _is_new_or_changed(resolved, before):
            continue
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        metadata_id = str(payload.get("id") or "").strip()
        if metadata_id != expected_video_id:
            raise YtDlpError(
                "yt-dlp info JSON metadata id does not match the requested video id"
            )
        if str(payload.get("title") or "").strip():
            return resolved
    raise YtDlpError("yt-dlp completed but did not produce a valid info JSON")


def download_video(
    source: SourceInput | str,
    output_dir: Path,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 3600.0,
    cancel_event: Event | None = None,
) -> YtDlpDownload:
    """Download one YouTube video and return the path yt-dlp actually wrote."""

    normalized = _coerce_youtube(source)
    output_dir = Path(output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir = output_dir.resolve(strict=True)
    if cancel_event is not None and cancel_event.is_set():
        raise YtDlpCancelled("yt-dlp download cancelled before start")
    before = _snapshot_media(output_dir)
    command = build_download_command(normalized.normalized_value, output_dir)
    try:
        if runner is subprocess.run:
            completed = run_process(
                command,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        else:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
                shell=False,
            )
    except ProcessCancelled as error:
        raise YtDlpCancelled(str(error)) from error
    except ProcessTimedOut as error:
        raise YtDlpTimeout(str(error)) from error
    except ProcessUnavailable as error:
        raise YtDlpError("yt-dlp executable is unavailable") from error
    except ProcessError as error:
        raise YtDlpError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise YtDlpTimeout(f"yt-dlp timed out after {timeout_seconds:.1f}s") from error
    except FileNotFoundError as error:
        raise YtDlpError("yt-dlp executable is unavailable") from error
    if cancel_event is not None and cancel_event.is_set():
        raise YtDlpCancelled("yt-dlp download cancelled")
    returncode = int(getattr(completed, "returncode", 1))
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if returncode != 0:
        detail = (stderr or stdout or "unknown yt-dlp failure").strip()
        raise YtDlpError(f"yt-dlp failed: {detail}")
    media_path = _printed_media_path(stdout, output_dir, before) or _discover_media_path(
        output_dir, before
    )
    if media_path is None:
        raise YtDlpError("yt-dlp completed but did not produce a media file")
    info_path = _locate_info_path(media_path, output_dir, before, normalized.video_id)
    return YtDlpDownload(
        media_path=media_path,
        stdout=stdout,
        stderr=stderr,
        info_path=info_path if info_path.is_file() else None,
    )


__all__ = [
    "YtDlpCancelled",
    "YtDlpDownload",
    "YtDlpError",
    "YtDlpTimeout",
    "build_download_command",
    "download_video",
]
