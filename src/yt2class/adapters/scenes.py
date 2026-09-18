"""Scene detection, source-faithful frame extraction, and frame metrics."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
import re
import subprocess
from threading import Event
from typing import Callable, Literal

from PIL import Image, ImageFilter

from yt2class.adapters.process import (
    ProcessCancelled,
    ProcessError,
    ProcessTimedOut,
    ProcessUnavailable,
    run_process,
)
from yt2class.orchestration.concurrency import map_parallel
from yt2class.domain.visual import (
    FrameOccurrence,
    FrameQuality,
    Scene,
    VisualAsset,
    VisualCatalogue,
    VisualGap,
    is_accepted_visual_occurrence,
)


class SceneError(RuntimeError):
    """Raised when scene detection or frame extraction cannot proceed."""


class SceneCancelled(SceneError):
    """Raised when a frame extraction is cancelled by the caller."""


SceneProfile = Literal["content", "adaptive"]


@dataclass(frozen=True)
class SceneRange:
    id: str
    start_seconds: float
    end_seconds: float
    detector: SceneProfile


@dataclass(frozen=True)
class FrameSample:
    id: str
    scene_id: str
    requested_seconds: float


@dataclass(frozen=True)
class FrameExtraction:
    """Published frame path plus the decoded source PTS, when available."""

    path: Path
    actual_source_seconds: float | None
    quality_flags: tuple[str, ...] = ()


_SHOWINFO_PTS = re.compile(r"\bpts_time:(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+))\b")


Runner = Callable[..., object]


def build_frame_command(
    video_path: Path,
    *,
    requested_seconds: float,
    output_path: Path,
) -> list[str]:
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "info",
        "-nostdin",
        "-y",
        "-copyts",
        "-start_at_zero",
        "-ss",
        f"{float(requested_seconds):.6f}",
        "-i",
        str(Path(video_path)),
        "-vf",
        "scale='min(iw,1280)':-2,showinfo",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(Path(output_path)),
    ]


def _run_frame_command(
    command: list[str],
    *,
    runner: Runner,
    timeout_seconds: float,
    cancel_event: Event | None,
) -> object:
    try:
        if runner is subprocess.run:
            return run_process(
                command,
                timeout_seconds=timeout_seconds,
                cancel_event=cancel_event,
            )
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except ProcessCancelled as error:
        raise SceneCancelled(f"frame extraction cancelled: {error}") from error
    except ProcessTimedOut as error:
        raise SceneError(f"frame extraction timed out: {error}") from error
    except ProcessUnavailable as error:
        raise SceneError("ffmpeg executable is unavailable") from error
    except ProcessError as error:
        raise SceneError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise SceneError(f"frame extraction timed out after {timeout_seconds:.1f}s") from error
    except FileNotFoundError as error:
        raise SceneError("ffmpeg executable is unavailable") from error


def _decoded_pts(completed: object) -> float | None:
    stderr = str(getattr(completed, "stderr", "") or "")
    values: list[float] = []
    for match in _SHOWINFO_PTS.finditer(stderr):
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        if math.isfinite(value) and value >= 0:
            values.append(value)
    return values[0] if values else None


def extract_frame_with_timestamp(
    video_path: Path,
    sample: FrameSample,
    output_path: Path,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 120.0,
    cancel_event: Event | None = None,
) -> FrameExtraction:
    """Extract one frame and publish the decoded source PTS when FFmpeg reports it."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
    temporary.unlink(missing_ok=True)
    try:
        completed = _run_frame_command(
            build_frame_command(
                video_path,
                requested_seconds=sample.requested_seconds,
                output_path=temporary,
            ),
            runner=runner,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
    except SceneCancelled:
        temporary.unlink(missing_ok=True)
        raise
    except SceneError:
        temporary.unlink(missing_ok=True)
        raise
    if cancel_event is not None and cancel_event.is_set():
        temporary.unlink(missing_ok=True)
        raise SceneCancelled("frame extraction cancelled")
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = str(getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "unknown failure")
        temporary.unlink(missing_ok=True)
        raise SceneError(f"ffmpeg frame extraction failed: {detail.strip()}")
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise SceneError(f"ffmpeg did not produce frame at {sample.requested_seconds:.3f}s")
    temporary.replace(output_path)
    actual = _decoded_pts(completed)
    return FrameExtraction(
        path=output_path,
        actual_source_seconds=actual,
        quality_flags=() if actual is not None else ("actual-timestamp-unavailable",),
    )


def extract_frame(
    video_path: Path,
    sample: FrameSample,
    output_path: Path,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 120.0,
    cancel_event: Event | None = None,
) -> Path:
    """Compatibility wrapper returning only the published frame path."""

    return extract_frame_with_timestamp(
        video_path,
        sample,
        output_path,
        runner=runner,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
    ).path


def _detect_with_scenedetect(
    video_path: Path,
    *,
    profile: SceneProfile,
    content_threshold: float,
    min_scene_len: int,
) -> list[tuple[float, float]]:
    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import AdaptiveDetector, ContentDetector

    video = open_video(str(video_path))
    manager = SceneManager()
    detector = (
        ContentDetector(threshold=content_threshold, min_scene_len=min_scene_len)
        if profile == "content"
        else AdaptiveDetector(adaptive_threshold=content_threshold, min_scene_len=min_scene_len)
    )
    manager.add_detector(detector)
    manager.detect_scenes(video=video)
    return [
        (scene[0].get_seconds(), scene[1].get_seconds())
        for scene in manager.get_scene_list(start_in_scene=True)
    ]


def detect_scene_ranges(
    video_path: Path,
    *,
    duration_seconds: float,
    profile: SceneProfile = "content",
    content_threshold: float = 10.0,
    min_scene_len: int = 15,
    detector: Callable[..., list[tuple[float, float]]] | None = None,
) -> list[SceneRange]:
    """Detect bounded source ranges, falling back to one full static scene."""

    if duration_seconds <= 0:
        raise SceneError("scene duration must be positive")
    try:
        if detector is not None:
            try:
                raw = detector(
                    Path(video_path),
                    profile=profile,
                    content_threshold=content_threshold,
                    min_scene_len=min_scene_len,
                )
            except TypeError:
                raw = detector(Path(video_path))
        else:
            raw = _detect_with_scenedetect(
                Path(video_path),
                profile=profile,
                content_threshold=content_threshold,
                min_scene_len=min_scene_len,
            )
    except Exception as error:
        if isinstance(error, SceneCancelled):
            raise
        if isinstance(error, ProcessCancelled):
            raise SceneCancelled(f"scene detection cancelled: {error}") from error
        raise SceneError(f"scene detection failed: {error}") from error
    candidates: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, SceneRange):
            start, end = item.start_seconds, item.end_seconds
        elif hasattr(item, "start_seconds") and hasattr(item, "end_seconds"):
            start, end = float(item.start_seconds), float(item.end_seconds)
        else:
            start, end = item
        start = max(0.0, float(start))
        end = min(duration_seconds, float(end))
        if start < end:
            candidates.append((start, end))

    # Detectors can return duplicate, out-of-order, overlapping, or sparse
    # ranges.  Normalize them into one stable half-open partition so every
    # downstream timestamp has exactly one scene and no timeline is silently
    # dropped.
    candidates.sort(key=lambda item: (item[0], item[1]))
    ranges: list[SceneRange] = []
    cursor = 0.0
    for start, end in candidates:
        if end <= cursor:
            continue
        if start > cursor:
            ranges.append(SceneRange(f"scene-{len(ranges)+1:04d}", cursor, start, profile))
        start = max(start, cursor)
        if start < end:
            ranges.append(SceneRange(f"scene-{len(ranges)+1:04d}", start, end, profile))
            cursor = end
    if cursor < duration_seconds:
        ranges.append(SceneRange(f"scene-{len(ranges)+1:04d}", cursor, duration_seconds, profile))
    if not ranges:
        ranges = [SceneRange("scene-0001", 0.0, duration_seconds, profile)]
    return ranges


def sample_scene_times(
    scenes: list[SceneRange],
    *,
    duration_seconds: float,
    static_interval_seconds: float = 30.0,
) -> list[FrameSample]:
    """Sample each scene at 20/50/80 percent and fill long static gaps."""

    if duration_seconds <= 0:
        raise SceneError("sample duration must be positive")
    selected: list[tuple[str, float]] = []
    for scene in scenes:
        length = scene.end_seconds - scene.start_seconds
        if length <= 0:
            continue
        ratios = (0.5,) if length < 1.0 else (0.2, 0.5, 0.8)
        for ratio in ratios:
            selected.append((scene.id, scene.start_seconds + length * ratio))
        if static_interval_seconds > 0 and length > static_interval_seconds:
            cursor = scene.start_seconds + static_interval_seconds
            while cursor < scene.end_seconds:
                selected.append((scene.id, cursor))
                cursor += static_interval_seconds
    unique: dict[tuple[str, float], None] = {}
    for scene_id, timestamp in selected:
        timestamp = min(duration_seconds - 1e-6, max(0.0, timestamp))
        unique[(scene_id, round(timestamp, 6))] = None
    ordered = sorted(unique, key=lambda item: (item[1], item[0]))
    return [
        FrameSample(
            id=f"sample-{index:04d}",
            scene_id=scene_id,
            requested_seconds=timestamp,
        )
        for index, (scene_id, timestamp) in enumerate(ordered, start=1)
    ]


def file_sha256(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _average_hash(path: Path) -> int:
    image = Image.open(path).convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(image.tobytes())
    average = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= average)
    return value


def measure_frame_quality(path: Path, *, ocr_text: str = "") -> FrameQuality:
    try:
        image = Image.open(path).convert("L")
    except (OSError, ValueError) as error:
        raise SceneError(f"cannot decode extracted frame: {path}") from error
    width, height = image.size
    pixels = list(image.tobytes())
    brightness = sum(pixels) / (255.0 * len(pixels))
    edges = image.filter(ImageFilter.FIND_EDGES)
    edge_pixels = list(edges.tobytes())
    edge_mean = sum(edge_pixels) / len(edge_pixels)
    edge_variance = sum((pixel - edge_mean) ** 2 for pixel in edge_pixels) / len(edge_pixels)
    sharpness = min(1.0, math.sqrt(edge_variance) / 64.0)
    density = min(1.0, len(ocr_text.strip()) / max(1.0, (width * height) / 1000.0))
    return FrameQuality(
        width=width,
        height=height,
        brightness=brightness,
        sharpness=sharpness,
        ocr_density=density,
    )


def _cluster_id(hash_value: int, existing: list[tuple[int, str]], threshold: int = 8) -> str:
    for previous, cluster in existing:
        if (hash_value ^ previous).bit_count() <= threshold:
            return cluster
    return f"cluster-{hash_value:016x}"


def extract_visual_catalogue(
    *,
    source_id: str,
    video_path: Path,
    run_root: Path,
    duration_seconds: float,
    profile: SceneProfile = "content",
    detector: Callable[..., list[tuple[float, float]]] | None = None,
    runner: Runner = subprocess.run,
    static_interval_seconds: float = 30.0,
    cancel_event: Event | None = None,
) -> VisualCatalogue:
    """Extract all scheduled occurrences without applying a page-count budget."""

    frames_dir = Path(run_root) / "frames"
    scenes = detect_scene_ranges(
        video_path,
        duration_seconds=duration_seconds,
        profile=profile,
        detector=detector,
    )
    samples = sample_scene_times(
        scenes,
        duration_seconds=duration_seconds,
        static_interval_seconds=static_interval_seconds,
    )
    model_scenes = [
        Scene(
            id=scene.id,
            start_seconds=scene.start_seconds,
            end_seconds=scene.end_seconds,
            detector=scene.detector,
        )
        for scene in scenes
    ]
    assets: list[VisualAsset] = []
    occurrences: list[FrameOccurrence] = []
    gaps = []
    asset_by_hash: dict[str, str] = {}
    clusters: list[tuple[int, str]] = []

    def _extract_sample(sample: FrameSample) -> dict[str, object] | None:
        frame_path = frames_dir / f"{sample.id}_{sample.requested_seconds:.3f}s.jpg"
        try:
            extraction = extract_frame_with_timestamp(
                video_path,
                sample,
                frame_path,
                runner=runner,
                cancel_event=cancel_event,
            )
        except ProcessCancelled:
            raise
        except SceneCancelled:
            raise
        except (ProcessError, ProcessTimedOut, ProcessUnavailable, SceneError):
            return None
        digest = file_sha256(frame_path)
        quality = measure_frame_quality(frame_path)
        return {
            "sample": sample,
            "frame_path": frame_path,
            "extraction": extraction,
            "digest": digest,
            "quality": quality,
        }

    extracted = [item for item in map_parallel(samples, _extract_sample, cancel_event=cancel_event) if item]
    for item in extracted:
        sample = item["sample"]
        frame_path = item["frame_path"]
        extraction = item["extraction"]
        digest = item["digest"]
        quality = item["quality"]
        asset_id = asset_by_hash.get(digest)
        if asset_id is None:
            asset_id = f"asset-{digest[:16]}"
            asset_by_hash[digest] = asset_id
            relative_path = frame_path.resolve().relative_to(Path(run_root).resolve()).as_posix()
            assets.append(
                VisualAsset(
                    id=asset_id,
                    role="frame",
                    path=relative_path,
                    sha256=digest,
                    mime_type="image/jpeg",
                    width=quality.width,
                    height=quality.height,
                )
            )
        image_hash = _average_hash(frame_path)
        cluster = _cluster_id(image_hash, clusters)
        if not any(existing == cluster for _, existing in clusters):
            clusters.append((image_hash, cluster))
        occurrence_id = f"occ-{len(occurrences)+1:04d}"
        reject_reason = None
        quality_flags: list[str] = list(extraction.quality_flags)
        actual_source_seconds = extraction.actual_source_seconds
        if actual_source_seconds is not None and actual_source_seconds >= duration_seconds:
            actual_source_seconds = max(0.0, duration_seconds - 1e-6)
            quality_flags.append("actual-timestamp-clamped")
        occurrence_scene_id = sample.scene_id
        if actual_source_seconds is not None:
            for scene in model_scenes:
                if scene.start_seconds <= actual_source_seconds < scene.end_seconds:
                    occurrence_scene_id = scene.id
                    break
        if quality.brightness < 0.03 and quality.sharpness < 0.01:
            reject_reason = "near-black/low-variation"
            quality_flags.append("near-black")
        elif quality.sharpness < 0.01:
            reject_reason = "low-sharpness"
            quality_flags.append("low-sharpness")
        occurrences.append(
            FrameOccurrence(
                id=occurrence_id,
                scene_id=occurrence_scene_id,
                asset_id=asset_id,
                requested_seconds=sample.requested_seconds,
                timestamp_seconds=actual_source_seconds,
                actual_source_seconds=actual_source_seconds,
                cluster_id=cluster,
                reject_reason=reject_reason,
                quality=quality,
                quality_flags=quality_flags,
            )
        )
    for sample in samples:
        if any(item["sample"].id == sample.id for item in extracted):
            continue
        gaps.append(
            {
                "id": f"visual-gap-{len(gaps)+1:04d}",
                "start_seconds": max(0.0, sample.requested_seconds - 0.5),
                "end_seconds": min(duration_seconds, sample.requested_seconds + 0.5),
                "reason": "frame extraction failed",
            }
        )
    assets_by_id = {asset.id: asset for asset in assets}
    covered_scenes = {
        occurrence.scene_id
        for occurrence in occurrences
        if is_accepted_visual_occurrence(occurrence, assets_by_id)
    }
    for scene in model_scenes:
        if scene.id in covered_scenes:
            continue
        gaps.append(
            {
                "id": f"visual-gap-{len(gaps)+1:04d}",
                "start_seconds": scene.start_seconds,
                "end_seconds": scene.end_seconds,
                "reason": "no accepted visual occurrence",
            }
        )

    visual_gaps = [VisualGap.model_validate(gap) for gap in gaps if gap["start_seconds"] < gap["end_seconds"]]
    missing_decoded_pts = any(
        "actual-timestamp-unavailable" in occurrence.quality_flags
        for occurrence in occurrences
    )
    return VisualCatalogue(
        schema_version="1.0",
        source_id=source_id,
        scenes=model_scenes,
        assets=assets,
        occurrences=occurrences,
        ocr_regions=[],
        profile=profile,
        status="complete" if not visual_gaps and not missing_decoded_pts else "degraded",
        gaps=visual_gaps,
    )


__all__ = [
    "FrameSample",
    "FrameExtraction",
    "SceneError",
    "SceneCancelled",
    "SceneRange",
    "build_frame_command",
    "detect_scene_ranges",
    "extract_frame",
    "extract_frame_with_timestamp",
    "extract_visual_catalogue",
    "file_sha256",
    "measure_frame_quality",
    "sample_scene_times",
]
