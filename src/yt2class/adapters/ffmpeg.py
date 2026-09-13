"""Controlled FFmpeg and ffprobe adapters with media provenance."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import subprocess
from threading import Event
from typing import Callable

from yt2class.domain.source import (
    MediaStream,
    SourceInputError,
    TransformKind,
    Transformation,
    content_sha256,
)
from yt2class.adapters.process import (
    ProcessCancelled,
    ProcessError,
    ProcessTimedOut,
    ProcessUnavailable,
    run_process,
)


class FfmpegError(RuntimeError):
    """Raised when ffmpeg/ffprobe cannot produce a valid media result."""


class FfmpegCancelled(FfmpegError):
    """Raised when a media command is cancelled."""


class FfmpegTimeout(FfmpegError):
    """Raised when a media command exceeds its subprocess timeout."""


@dataclass(frozen=True)
class MediaProbe:
    path: Path
    duration_seconds: float
    streams: tuple[MediaStream, ...]
    rotation_degrees: int | None
    fps: float | None
    timebase: str


@dataclass(frozen=True)
class TransformResult:
    output_path: Path
    transformation: Transformation
    stdout: str = ""
    stderr: str = ""


Runner = Callable[..., object]


def build_ffprobe_command(path: Path) -> list[str]:
    """Build a JSON ffprobe command for duration, streams, and timing metadata."""

    return [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(Path(path)),
    ]


def _parse_ratio(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in {"0/0", "N/A"}:
        return None
    try:
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            result = float(numerator) / float(denominator)
        else:
            result = float(text)
    except (ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _rotation(stream: dict[str, object]) -> int | None:
    tags = stream.get("tags") or {}
    if isinstance(tags, dict) and tags.get("rotate") not in (None, ""):
        try:
            return int(float(str(tags["rotate"])))
        except ValueError:
            pass
    for side_data in stream.get("side_data_list") or []:
        if isinstance(side_data, dict) and side_data.get("rotation") not in (None, ""):
            try:
                return int(float(str(side_data["rotation"])))
            except ValueError:
                pass
    return None


def parse_ffprobe_json(payload: dict[str, object], *, path: Path) -> MediaProbe:
    """Parse ffprobe's JSON response and require one usable video stream."""

    try:
        duration = float(str((payload.get("format") or {}).get("duration")))
    except (AttributeError, TypeError, ValueError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        raise FfmpegError(f"ffprobe returned an invalid duration for {path}")

    raw_streams = payload.get("streams")
    if not isinstance(raw_streams, list):
        raise FfmpegError(f"ffprobe returned no streams for {path}")
    streams: list[MediaStream] = []
    for raw in raw_streams:
        if not isinstance(raw, dict):
            continue
        codec_type = raw.get("codec_type")
        if codec_type not in {"video", "audio", "subtitle"}:
            continue
        codec_name = str(raw.get("codec_name") or "").strip()
        if not codec_name:
            raise FfmpegError(f"ffprobe stream {raw.get('index', '?')} has no codec")
        try:
            index = int(raw.get("index", len(streams)))
        except (TypeError, ValueError) as error:
            raise FfmpegError(f"ffprobe returned an invalid stream index for {path}") from error
        tags = raw.get("tags") or {}
        language = None
        if isinstance(tags, dict):
            raw_language = str(tags.get("language") or "").strip()
            language = raw_language or None
        fps = _parse_ratio(raw.get("avg_frame_rate")) or _parse_ratio(raw.get("r_frame_rate"))
        timebase = str(raw.get("time_base") or "").strip() or None
        streams.append(
            MediaStream(
                index=index,
                codec_type=codec_type,
                codec_name=codec_name,
                language=language,
                rotation_degrees=_rotation(raw),
                fps=fps,
                timebase=timebase,
            )
        )
    video_streams = [stream for stream in streams if stream.codec_type == "video"]
    if not video_streams:
        raise FfmpegError(f"ffprobe found no video stream for {path}")
    primary = video_streams[0]
    if primary.timebase is None:
        raise FfmpegError(f"ffprobe video stream has no timebase for {path}")
    return MediaProbe(
        path=Path(path),
        duration_seconds=duration,
        streams=tuple(streams),
        rotation_degrees=primary.rotation_degrees,
        fps=primary.fps,
        timebase=primary.timebase,
    )


def probe_media(
    path: Path,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 120.0,
    cancel_event: Event | None = None,
) -> MediaProbe:
    """Run ffprobe without a shell and validate the media timing contract."""

    path = Path(path)
    if cancel_event is not None and cancel_event.is_set():
        raise FfmpegCancelled("ffprobe cancelled before start")
    command = build_ffprobe_command(path)
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
        raise FfmpegCancelled(str(error)) from error
    except ProcessTimedOut as error:
        raise FfmpegTimeout(str(error)) from error
    except ProcessUnavailable as error:
        raise FfmpegError("ffprobe executable is unavailable") from error
    except ProcessError as error:
        raise FfmpegError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise FfmpegTimeout(f"ffprobe timed out after {timeout_seconds:.1f}s") from error
    except FileNotFoundError as error:
        raise FfmpegError("ffprobe executable is unavailable") from error
    if cancel_event is not None and cancel_event.is_set():
        raise FfmpegCancelled("ffprobe cancelled")
    stdout = str(getattr(completed, "stdout", "") or "")
    returncode = int(getattr(completed, "returncode", 1))
    if returncode != 0:
        stderr = str(getattr(completed, "stderr", "") or "").strip()
        raise FfmpegError(f"ffprobe failed: {stderr or 'unknown failure'}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise FfmpegError("ffprobe returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise FfmpegError("ffprobe returned a non-object JSON payload")
    return parse_ffprobe_json(payload, path=path)


def _validate_transform_range(start_seconds: float, end_seconds: float | None) -> tuple[float, float | None]:
    try:
        start = float(start_seconds)
    except (TypeError, ValueError) as error:
        raise FfmpegError("transform start must be numeric") from error
    if not math.isfinite(start) or start < 0:
        raise FfmpegError("transform start must be finite and non-negative")
    if end_seconds is None:
        return start, None
    try:
        end = float(end_seconds)
    except (TypeError, ValueError) as error:
        raise FfmpegError("transform end must be numeric") from error
    if not math.isfinite(end) or end <= start:
        raise FfmpegError("transform end must be finite and greater than start")
    return start, end


def _atempo_chain(speed_ratio: float) -> str:
    """Represent any positive tempo as filters within FFmpeg's 0.5..2 range."""

    remaining = speed_ratio
    factors: list[float] = []
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    if abs(remaining - 1.0) > 1e-9:
        factors.append(remaining)
    return ",".join(f"atempo={factor:.8g}" for factor in factors)


def build_transform_command(
    parent_path: Path,
    output_path: Path,
    *,
    kind: TransformKind,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    speed_ratio: float = 1.0,
) -> list[str]:
    """Build a deterministic FFmpeg command for a derived media artifact."""

    start, end = _validate_transform_range(start_seconds, end_seconds)
    try:
        speed = float(speed_ratio)
    except (TypeError, ValueError) as error:
        raise FfmpegError("speed ratio must be numeric") from error
    if not math.isfinite(speed) or speed <= 0:
        raise FfmpegError("speed ratio must be finite and greater than zero")
    if kind not in {"copy", "proxy", "clip", "audio-extract"}:
        raise FfmpegError(f"unsupported transform kind: {kind}")
    if kind == "copy" and speed != 1.0:
        raise FfmpegError("copy transform cannot change speed without re-encoding")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-ss",
        f"{start:.6f}",
    ]
    if end is not None:
        command.extend(["-to", f"{end:.6f}"])
    if kind == "audio-extract":
        command.extend(["-i", str(Path(parent_path)), "-map", "0:a:0?", "-vn"])
        if speed != 1.0:
            command.extend(["-filter:a", _atempo_chain(speed)])
        command.extend(["-c:a", "pcm_s16le"])
    elif kind == "proxy" or (kind == "clip" and speed != 1.0):
        command.extend(
            [
                "-i",
                str(Path(parent_path)),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
            ]
        )
        command.extend(["-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac"])
        if speed != 1.0:
            command.extend(["-filter:v", f"setpts=PTS/{speed:.8g}"])
            command.extend(["-filter:a", _atempo_chain(speed)])
    else:
        command.extend(
            [
                "-i",
                str(Path(parent_path)),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
            ]
        )
        command.extend(["-c", "copy"])
    command.append(str(Path(output_path)))
    return command


def _command_digest(command: list[str]) -> str:
    canonical = json.dumps(command, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_transform(
    parent_path: Path,
    output_path: Path,
    *,
    kind: TransformKind,
    start_seconds: float = 0.0,
    end_seconds: float | None = None,
    speed_ratio: float = 1.0,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 3600.0,
    cancel_event: Event | None = None,
) -> TransformResult:
    """Run one transformation atomically and record its source provenance."""

    parent_path = Path(parent_path)
    output_path = Path(output_path)
    if cancel_event is not None and cancel_event.is_set():
        raise FfmpegCancelled("ffmpeg transform cancelled before start")
    try:
        if not parent_path.is_file() or parent_path.stat().st_size == 0:
            raise FfmpegError(f"parent media is missing or empty: {parent_path}")
        parent_hash = content_sha256(parent_path)
    except OSError as error:
        raise FfmpegError(f"cannot read parent media: {parent_path}") from error
    except SourceInputError as error:
        raise FfmpegError(f"cannot read parent media: {parent_path}") from error
    try:
        if output_path.resolve() == parent_path.resolve():
            raise FfmpegError("transform output must differ from parent media")
    except OSError as error:
        raise FfmpegError("cannot resolve transform paths") from error
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_output = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
    temp_output.unlink(missing_ok=True)
    command = build_transform_command(
        parent_path,
        temp_output,
        kind=kind,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
        speed_ratio=speed_ratio,
    )
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
        temp_output.unlink(missing_ok=True)
        raise FfmpegCancelled(str(error)) from error
    except ProcessTimedOut as error:
        temp_output.unlink(missing_ok=True)
        raise FfmpegTimeout(str(error)) from error
    except ProcessUnavailable as error:
        temp_output.unlink(missing_ok=True)
        raise FfmpegError("ffmpeg executable is unavailable") from error
    except ProcessError as error:
        temp_output.unlink(missing_ok=True)
        raise FfmpegError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        temp_output.unlink(missing_ok=True)
        raise FfmpegTimeout(f"ffmpeg timed out after {timeout_seconds:.1f}s") from error
    except FileNotFoundError as error:
        temp_output.unlink(missing_ok=True)
        raise FfmpegError("ffmpeg executable is unavailable") from error
    if cancel_event is not None and cancel_event.is_set():
        temp_output.unlink(missing_ok=True)
        raise FfmpegCancelled("ffmpeg transform cancelled")
    returncode = int(getattr(completed, "returncode", 1))
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if returncode != 0:
        temp_output.unlink(missing_ok=True)
        detail = (stderr or stdout or "unknown failure").strip()
        raise FfmpegError(f"ffmpeg failed: {detail}")
    if not temp_output.is_file() or temp_output.stat().st_size == 0:
        temp_output.unlink(missing_ok=True)
        raise FfmpegError("ffmpeg completed but did not produce a media file")
    transformation = Transformation(
        kind=kind,
        parent_hash=parent_hash,
        command_digest=_command_digest(command),
        time_offset_seconds=float(start_seconds),
        speed_ratio=float(speed_ratio),
    )
    temp_output.replace(output_path)
    return TransformResult(
        output_path=output_path,
        transformation=transformation,
        stdout=stdout,
        stderr=stderr,
    )


__all__ = [
    "FfmpegCancelled",
    "FfmpegError",
    "FfmpegTimeout",
    "MediaProbe",
    "TransformResult",
    "build_ffprobe_command",
    "build_transform_command",
    "parse_ffprobe_json",
    "probe_media",
    "run_transform",
]
