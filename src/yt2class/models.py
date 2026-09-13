"""Shared immutable data models."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunPaths:
    """Directories and stable output paths for one source lesson."""

    root: Path
    media_dir: Path
    frames_dir: Path
    analysis_dir: Path
    source_video: Path
    manifest_path: Path
    deck_spec_path: Path
    pptx_path: Path
