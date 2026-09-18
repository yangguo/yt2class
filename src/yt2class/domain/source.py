"""SourceManifest: immutable media identity produced by ingestion."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import re
from typing import Literal, Self
from urllib.parse import parse_qs, urlsplit

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    PositiveSeconds,
    RelativePath,
    Seconds,
    StrictModel,
    unique_ids,
)

SourceKind = Literal["youtube", "local"]
LocalMode = Literal["copy", "reference"]
StreamType = Literal["video", "audio", "subtitle"]
TransformKind = Literal["copy", "proxy", "clip", "audio-extract"]

SUPPORTED_LOCAL_SUFFIXES = frozenset({".mp4", ".mkv", ".webm", ".mov"})
YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class SourceInputError(ValueError):
    """Raised when a source input is unsafe or cannot identify one video."""


def _normalize_youtube(value: str) -> tuple[str, str]:
    raw = value.strip()
    parsed = urlsplit(raw)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or hostname not in YOUTUBE_HOSTS:
        raise SourceInputError("only YouTube URLs are supported")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "list" in query or parsed.path.rstrip("/").lower() == "/playlist":
        raise SourceInputError("playlist URLs are not supported; provide one video URL")

    if hostname == "youtu.be":
        video_id = parsed.path.strip("/").split("/", 1)[0]
    elif parsed.path.rstrip("/").lower() == "/watch":
        values = query.get("v", [])
        video_id = values[0] if len(values) == 1 else ""
    else:
        parts = [part for part in parsed.path.split("/") if part]
        video_id = parts[1] if len(parts) >= 2 and parts[0].lower() in {"shorts", "embed", "live"} else ""

    if not _VIDEO_ID_PATTERN.fullmatch(video_id):
        raise SourceInputError("YouTube URL does not contain one valid video id")
    return f"https://www.youtube.com/watch?v={video_id}", video_id


_SHA256_BY_PATH: dict[str, tuple[tuple[str, int, int, int, int], str]] = {}


def clear_content_sha256_cache() -> None:
    """Test helper: drop in-process stat memoization."""

    _SHA256_BY_PATH.clear()


def content_sha256(path: Path, *, fresh: bool = False) -> str:
    """Hash file bytes without loading a media file into memory.

    Reuses the digest for unchanged inode/size/mtime/ctime within the process unless
    ``fresh=True`` (use after mutating a file when proving it did not change).
    """

    try:
        resolved = path.expanduser().resolve(strict=False)
        stat = resolved.stat()
    except OSError as error:
        raise SourceInputError(f"cannot read source file: {path}") from error
    key = (
        str(resolved),
        int(stat.st_ino),
        int(stat.st_size),
        int(stat.st_mtime_ns),
        int(getattr(stat, "st_ctime_ns", stat.st_ctime)),
    )
    path_key = str(resolved)
    if not fresh:
        cached = _SHA256_BY_PATH.get(path_key)
        if cached is not None and cached[0] == key:
            return cached[1]
    digest = sha256()
    try:
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise SourceInputError(f"cannot read source file: {path}") from error
    value = digest.hexdigest()
    _SHA256_BY_PATH[path_key] = (key, value)
    return value


def _validate_local_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise SourceInputError(f"local source does not exist: {path}") from error
    if not resolved.is_file():
        raise SourceInputError(f"local source is not a regular file: {path}")
    if resolved.stat().st_size == 0:
        raise SourceInputError(f"local source is empty: {path}")
    if resolved.suffix.lower() not in SUPPORTED_LOCAL_SUFFIXES:
        raise SourceInputError(
            f"unsupported local media container {resolved.suffix or '(none)'}; "
            f"expected {sorted(SUPPORTED_LOCAL_SUFFIXES)}"
        )
    return resolved


class SourceInput(StrictModel):
    """One normalized YouTube or local video input before ingestion."""

    kind: SourceKind
    value: str = Field(min_length=1, max_length=2000)
    local_mode: LocalMode = "copy"

    @classmethod
    def from_value(cls, value: str | Path, *, local_mode: LocalMode = "copy") -> Self:
        text = str(value).strip()
        if not text:
            raise SourceInputError("source input is empty")
        parsed = urlsplit(text)
        if parsed.scheme or parsed.netloc:
            normalized, _ = _normalize_youtube(text)
            return cls(kind="youtube", value=normalized, local_mode="copy")
        path = _validate_local_path(text)
        return cls(kind="local", value=str(path), local_mode=local_mode)

    @model_validator(mode="after")
    def validate_value(self) -> Self:
        if self.kind == "youtube":
            normalized, _ = _normalize_youtube(self.value)
            if normalized != self.value:
                raise ValueError("youtube SourceInput.value must be normalized")
            if self.local_mode != "copy":
                raise ValueError("YouTube inputs cannot use local reference mode")
        else:
            _validate_local_path(self.value)
        return self

    @property
    def normalized_value(self) -> str:
        return self.value

    @property
    def video_id(self) -> str | None:
        if self.kind != "youtube":
            return None
        return _normalize_youtube(self.value)[1]

    @property
    def source_key(self) -> str:
        if self.kind == "youtube":
            return f"youtube:{self.video_id}"
        return f"local:{Path(self.value).resolve()}"

    @property
    def content_sha256(self) -> str | None:
        if self.kind == "youtube":
            return None
        return content_sha256(Path(self.value), fresh=True)


def read_source_inputs(path: Path, *, local_mode: LocalMode = "copy") -> list[SourceInput]:
    """Read a mixed batch list, preserving first occurrence order."""

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SourceInputError(f"cannot read source list: {path}") from error
    sources: list[SourceInput] = []
    seen: set[str] = set()
    for line in lines:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        source = SourceInput.from_value(value, local_mode=local_mode)
        if source.source_key not in seen:
            sources.append(source)
            seen.add(source.source_key)
    if not sources:
        raise SourceInputError(f"no sources found in {path}")
    return sources


class MediaStream(StrictModel):
    index: int = Field(ge=0)
    codec_type: StreamType
    codec_name: str = Field(min_length=1, max_length=64)
    language: str | None = Field(default=None, min_length=2, max_length=35)
    rotation_degrees: int | None = None
    fps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    timebase: str | None = Field(default=None, min_length=1, max_length=32)


class Transformation(StrictModel):
    kind: TransformKind
    parent_hash: Digest | None = None
    command_digest: Digest | None = None
    time_offset_seconds: Seconds = 0.0
    speed_ratio: float = Field(default=1.0, gt=0, allow_inf_nan=False)


class SourceManifest(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    kind: SourceKind
    title: str = Field(min_length=1, max_length=160)
    media_path: RelativePath
    sha256: Digest
    duration_seconds: PositiveSeconds
    url: str | None = None
    video_id: str | None = Field(default=None, min_length=1, max_length=32)
    streams: list[MediaStream] = Field(min_length=1)
    timebase: str = Field(min_length=1, max_length=32)
    local_mode: LocalMode = "copy"
    reference_path: str | None = Field(default=None, min_length=1, max_length=2000)
    transformations: list[Transformation] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_source_identity(self) -> Self:
        unique_ids((stream for stream in self.streams), attr="index", label="stream index")
        if self.kind == "youtube":
            if self.url is None or self.video_id is None:
                raise ValueError("youtube source requires url and video_id")
            if self.local_mode != "copy" or self.reference_path is not None:
                raise ValueError("youtube source cannot use local reference mode")
        elif self.url is not None or self.video_id is not None:
            raise ValueError("local source must not supply a remote url or video_id")
        if self.kind == "local" and self.local_mode == "reference" and self.reference_path is None:
            raise ValueError("local reference source requires reference_path")
        if self.kind == "local" and self.local_mode == "copy" and self.reference_path is not None:
            raise ValueError("local copy source must not supply reference_path")
        return self
