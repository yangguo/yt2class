"""Extract source subclips for native upload (never upload the full lecture file)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from yt2class.adapters.providers.native_video import NativeVideoError
from yt2class.stages.evidence_refinement import ClipExtractor, ExtractedClip

NativeClipMaterializer = Callable[[Path, float, float, Path, str], Path]


def materialize_native_clip(
    media_path: Path,
    start_seconds: float,
    end_seconds: float,
    output_dir: Path,
    upload_id: str,
    *,
    clip_extractor: ClipExtractor | None = None,
) -> Path:
    """Return a local file containing only ``[start_seconds, end_seconds)`` of the source."""

    if end_seconds <= start_seconds:
        raise NativeVideoError("empty native clip range")
    if not media_path.is_file():
        raise NativeVideoError(f"source media missing: {media_path}")
    dest = output_dir / "native-clips"
    dest.mkdir(parents=True, exist_ok=True)
    if clip_extractor is not None:
        extracted = clip_extractor(start_seconds, end_seconds, dest)
        if extracted is None:
            raise NativeVideoError("clip extraction failed")
        if isinstance(extracted, ExtractedClip):
            clip_path = Path(extracted.asset.path)
            if not clip_path.is_absolute():
                clip_path = output_dir / clip_path
            if not clip_path.is_file():
                raise NativeVideoError(f"extracted clip missing: {clip_path}")
            return clip_path
        if isinstance(extracted, Path):
            return extracted
        raise NativeVideoError("clip extractor returned unsupported type")
    try:
        from yt2class.adapters.ffmpeg import run_transform

        clip_path = dest / f"{upload_id}.mp4"
        run_transform(
            media_path,
            clip_path,
            kind="clip",
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        )
        return clip_path
    except Exception as error:
        raise NativeVideoError(f"native clip extraction unavailable: {error}") from error
