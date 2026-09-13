"""Courseware scene detection and perceptual frame de-duplication."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Iterable

from PIL import Image


@dataclass(frozen=True)
class FrameCandidate:
    """An exact source frame with stable provenance metadata."""

    frame_id: str
    timestamp: float
    path: Path
    source_sha256: str = ""


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _average_hash(path: Path) -> int:
    image = Image.open(path).convert("L").resize((8, 8), Image.Resampling.LANCZOS)
    pixels = list(image.tobytes())
    average = sum(pixels) / len(pixels)
    bits = 0
    for pixel in pixels:
        bits = (bits << 1) | int(pixel >= average)
    return bits


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def is_usable_frame(path: Path, *, min_mean_luma: float = 12.0, min_stddev: float = 2.0) -> bool:
    """Reject fade-to-black or otherwise near-uniform transition frames."""

    image = Image.open(path).convert("L").resize((64, 36), Image.Resampling.BILINEAR)
    pixels = list(image.tobytes())
    mean = sum(pixels) / len(pixels)
    variance = sum((pixel - mean) ** 2 for pixel in pixels) / len(pixels)
    return mean >= min_mean_luma and variance**0.5 >= min_stddev


def deduplicate_frames(
    candidates: Iterable[FrameCandidate], *, hamming_threshold: int = 8
) -> list[FrameCandidate]:
    """Keep the first frame from each visually distinct group, in time order."""

    kept: list[FrameCandidate] = []
    hashes: list[int] = []
    for candidate in candidates:
        current_hash = _average_hash(candidate.path)
        if any(
            _hamming_distance(current_hash, previous_hash) <= hamming_threshold
            for previous_hash in hashes
        ):
            continue
        kept.append(candidate)
        hashes.append(current_hash)
    return kept


def _video_duration_seconds(video_path: Path) -> float:
    import cv2

    capture = cv2.VideoCapture(str(video_path))
    try:
        fps = capture.get(cv2.CAP_PROP_FPS)
        frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
    finally:
        capture.release()
    return frames / fps if fps and frames else 0.0


def detect_scene_timestamps(
    video_path: Path,
    *,
    content_threshold: float = 10.0,
    min_scene_len: int = 30,
    max_static_gap: float = 90.0,
) -> list[float]:
    """Find content-change timestamps with a bounded fallback for static slides."""

    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(str(video_path))
    manager = SceneManager()
    manager.add_detector(
        ContentDetector(threshold=content_threshold, min_scene_len=min_scene_len)
    )
    manager.detect_scenes(video=video)
    scene_list = manager.get_scene_list(start_in_scene=True)

    duration = _video_duration_seconds(video_path)
    starts = [scene[0].get_seconds() for scene in scene_list]
    if not starts:
        starts = [0.0]
    elif starts[0] > 0.1:
        starts.insert(0, 0.0)

    timestamps: list[float] = []
    for index, start in enumerate(starts):
        if not timestamps or start - timestamps[-1] > 0.1:
            timestamps.append(start)
        next_start = starts[index + 1] if index + 1 < len(starts) else duration
        cursor = start + max_static_gap
        while duration and cursor < next_start - 0.1:
            timestamps.append(cursor)
            cursor += max_static_gap
    return sorted(set(round(timestamp, 3) for timestamp in timestamps if timestamp >= 0))


def extract_scene_candidates(
    video_path: Path,
    frames_dir: Path,
    *,
    hamming_threshold: int = 8,
    **detector_options: float | int,
) -> list[FrameCandidate]:
    """Extract and de-duplicate exact frames at detected scene timestamps."""

    from yt2class.media import extract_frame

    frames_dir.mkdir(parents=True, exist_ok=True)
    candidates: list[FrameCandidate] = []
    for index, timestamp in enumerate(
        detect_scene_timestamps(video_path, **detector_options), start=1
    ):
        frame_path = frames_dir / f"frame_{index:04d}_{timestamp:09.3f}.jpg"
        extract_frame(video_path, timestamp, frame_path)
        if not is_usable_frame(frame_path):
            continue
        candidates.append(
            FrameCandidate(
                frame_id=f"frame-{index:04d}",
                timestamp=timestamp,
                path=frame_path,
                source_sha256=file_sha256(frame_path),
            )
        )
    return deduplicate_frames(candidates, hamming_threshold=hamming_threshold)
