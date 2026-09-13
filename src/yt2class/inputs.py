"""Batch-input and run-directory helpers."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from yt2class.models import RunPaths


YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def validate_youtube_url(value: str) -> str:
    """Return a normalized YouTube URL or raise a clear input error."""

    url = value.strip()
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in YOUTUBE_HOSTS:
        raise ValueError(f"Only YouTube URLs are supported: {value}")
    return url


def read_urls(path: Path) -> list[str]:
    """Read non-empty, non-comment YouTube URLs in their original order."""

    candidates = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    urls = [validate_youtube_url(candidate) for candidate in candidates]
    if not urls:
        raise ValueError(f"No YouTube URLs found in {path}")
    return list(dict.fromkeys(urls))


def lesson_id(url: str) -> str:
    """Return a deterministic path-safe identifier for a source URL."""

    normalized = validate_youtube_url(url)
    return f"lesson-{sha256(normalized.encode('utf-8')).hexdigest()[:16]}"


def create_run_paths(output_root: Path, url: str) -> RunPaths:
    """Create and return a contained, restartable directory tree for a lesson."""

    output_root = output_root.expanduser().resolve()
    root = output_root / "runs" / lesson_id(url)
    if not root.is_relative_to(output_root):
        raise ValueError("Lesson output resolved outside the requested output root")

    media_dir = root / "media"
    frames_dir = root / "frames"
    analysis_dir = root / "analysis"
    for directory in (media_dir, frames_dir, analysis_dir):
        directory.mkdir(parents=True, exist_ok=True)

    return RunPaths(
        root=root,
        media_dir=media_dir,
        frames_dir=frames_dir,
        analysis_dir=analysis_dir,
        source_video=media_dir / "source.mp4",
        manifest_path=root / "manifest.json",
        deck_spec_path=analysis_dir / "deck.json",
        pptx_path=root / "lesson.pptx",
    )
