"""Optional steipete/summarize CLI adapter.

This module is a controlled subprocess + JSON contract, not a fork. It is not
wired into ``yt2class build``. Adoption is gated by
``docs/experiments/summarize-adoption.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal
import json
import subprocess

from pydantic import ConfigDict, Field, model_validator

from yt2class.domain.common import Seconds, StrictModel, validate_half_open

# Recorded 2026-09-13 from GitHub/npm, not from a live CLI run in this environment.
UPSTREAM_PACKAGE = "@steipete/summarize"
UPSTREAM_VERSION = "0.21.14"
UPSTREAM_COMMIT = "5b1f563a9649526a4d435f0a9ff5c8abe62e3748"
UPSTREAM_LICENSE = "MIT"
UPSTREAM_NODE = ">=24"
UPSTREAM_INSTALL = "npm i -g @steipete/summarize"
UPSTREAM_SLIDES_DOCS = "https://github.com/steipete/summarize/blob/main/docs/commands/slides.md"
UPSTREAM_DEFAULT_SLIDES_MAX = 6


class SummarizeError(RuntimeError):
    """Base error for summarize adapter failures."""


class SummarizeFailedError(SummarizeError):
    """CLI or envelope reported failure. Never treat as success."""


class SummarizeContractError(SummarizeError):
    """``ok: true`` payload that violates the ingestion contract."""


class SummarizeSlide(StrictModel):
    index: int = Field(ge=1)
    timestamp: Seconds
    image_path: str = Field(min_length=1)
    ocr_text: str | None = None
    ocr_confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class SummarizeExtractSegment(StrictModel):
    start: Seconds
    end: Seconds
    text: str = Field(min_length=1, max_length=8000)
    origin: Literal["sidecar", "manual-caption", "auto-caption", "asr", "unknown"]

    @model_validator(mode="after")
    def check_range(self) -> SummarizeExtractSegment:
        validate_half_open(self.start, self.end, label="extract segment")
        return self


class SummarizeExtract(StrictModel):
    source_url: str | None = None
    source_kind: Literal["youtube", "local", "url"]
    language: str | None = Field(default=None, min_length=2, max_length=35)
    text: str | None = Field(default=None, max_length=200000)
    segments: list[SummarizeExtractSegment] = Field(default_factory=list)


class SummarizeSlides(StrictModel):
    source_url: str | None = None
    source_kind: Literal["youtube", "local", "url"]
    slides_dir: str
    slides: list[SummarizeSlide] = Field(min_length=1)


class SummarizeResult(StrictModel):
    ok: Literal[True]
    slides: SummarizeSlides | None = None
    extract: SummarizeExtract | None = None
    exit_code: Literal[0] = 0


class NativeIngestExpectation(StrictModel):
    """Documented native yt-dlp/FFmpeg sample expectations for comparison."""

    source_kind: Literal["youtube", "local"]
    download_count: int = Field(ge=1)
    frame_timestamps: list[Seconds]
    caption_origin: Literal["sidecar", "manual-caption", "auto-caption", "asr", "none"]
    slides_capped_by_page_budget: bool = False


class LooseEnvelope(StrictModel):
    """Permissive raw CLI envelope. Extra upstream fields are ignored."""

    model_config = ConfigDict(extra="ignore", strict=False)

    ok: bool | None = None
    error: str | None = None
    slides: dict[str, Any] | None = None
    extract: dict[str, Any] | str | None = None


def build_slides_command(source: str, output_dir: Path, *, slides_ocr: bool = False) -> list[str]:
    command = ["summarize", "slides", "--json", "-o", str(output_dir)]
    if slides_ocr:
        command.append("--slides-ocr")
    command.extend(["--", source])
    return command


def build_extract_command(source: str) -> list[str]:
    return ["summarize", "--extract", "--json", "--timestamps", "--", source]


def classify_source(source: str) -> Literal["youtube", "local", "url"]:
    lowered = source.lower()
    if "youtube.com" in lowered or "youtu.be" in lowered:
        return "youtube"
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "url"
    return "local"


def _require_success(raw: dict[str, Any], *, exit_code: int) -> LooseEnvelope:
    if exit_code != 0:
        raise SummarizeFailedError(
            f"summarize exited {exit_code}; failures must not masquerade as success"
        )
    envelope = LooseEnvelope.model_validate(raw)
    if envelope.ok is not True:
        raise SummarizeFailedError(envelope.error or "summarize envelope ok is not true")
    return envelope


def _slides_from_raw(raw: dict[str, Any], *, source: str | None) -> SummarizeSlides:
    items = raw.get("slides")
    if not isinstance(items, list) or not items:
        raise SummarizeContractError("slides envelope is missing a non-empty slides list")
    parsed: list[SummarizeSlide] = []
    for item in items:
        if not isinstance(item, dict):
            raise SummarizeContractError("slide entry must be an object")
        image_path = item.get("imagePath") or item.get("image_path")
        if not isinstance(image_path, str) or ".." in Path(image_path).parts:
            raise SummarizeContractError("imagePath must not contain '..'")
        parsed.append(
            SummarizeSlide(
                index=item.get("index"),
                timestamp=item.get("timestamp"),
                image_path=image_path,
                ocr_text=item.get("ocrText", item.get("ocr_text")),
                ocr_confidence=item.get("ocrConfidence", item.get("ocr_confidence")),
            )
        )
    raw_source_url = raw.get("sourceUrl") or raw.get("source_url")
    source_url = source if source is not None else raw_source_url
    declared = raw.get("sourceKind") or raw.get("source_kind")
    kind = classify_source(source) if source is not None else (
        declared if declared in {"youtube", "local", "url"} else classify_source(str(source_url or ""))
    )
    slides_dir = raw.get("slidesDir") or raw.get("slides_dir")
    if not slides_dir:
        raise SummarizeContractError("slides envelope is missing slidesDir")
    return SummarizeSlides(
        source_url=source_url,
        source_kind=kind,
        slides_dir=str(slides_dir),
        slides=parsed,
    )


def _extract_from_raw(raw: dict[str, Any] | str, *, source: str | None) -> SummarizeExtract:
    if isinstance(raw, str):
        return SummarizeExtract(
            source_url=source,
            source_kind=classify_source(source or ""),
            text=raw,
            segments=[],
        )
    raw_source_url = raw.get("sourceUrl") or raw.get("source_url")
    source_url = source if source is not None else raw_source_url
    declared = raw.get("sourceKind") or raw.get("source_kind")
    kind = classify_source(source) if source is not None else (
        declared if declared in {"youtube", "local", "url"} else classify_source(str(source_url or ""))
    )
    segments = []
    for item in raw.get("segments") or []:
        segments.append(
            SummarizeExtractSegment(
                start=item.get("start", item.get("start_seconds")),
                end=item.get("end", item.get("end_seconds")),
                text=item.get("text") or item.get("text_original"),
                origin=item.get("origin") or "unknown",
            )
        )
    return SummarizeExtract(
        source_url=source_url,
        source_kind=kind,
        language=raw.get("language"),
        text=raw.get("text"),
        segments=segments,
    )


def parse_summarize_json(
    raw: dict[str, Any],
    *,
    exit_code: int = 0,
    source: str | None = None,
    frame_root: Path | None = None,
    duration_seconds: float | None = None,
) -> SummarizeResult:
    """Parse a fixture or CLI JSON envelope into a strict success result."""

    envelope = _require_success(raw, exit_code=exit_code)
    slides = _slides_from_raw(envelope.slides, source=source) if envelope.slides else None
    extract = _extract_from_raw(envelope.extract, source=source) if envelope.extract is not None else None
    if slides is None and extract is None:
        raise SummarizeContractError("successful summarize JSON must include slides or extract")
    result = SummarizeResult(ok=True, slides=slides, extract=extract, exit_code=0)
    validate_slide_times(result, duration_seconds=duration_seconds)
    if frame_root is not None:
        validate_frame_bytes(result, root=frame_root)
    return result


def validate_slide_times(result: SummarizeResult, *, duration_seconds: float | None = None) -> None:
    if result.slides is None:
        return
    for slide in result.slides.slides:
        if duration_seconds is not None and slide.timestamp >= duration_seconds:
            raise SummarizeContractError(
                f"slide {slide.index} timestamp {slide.timestamp} is not < duration {duration_seconds}"
            )


def validate_frame_bytes(result: SummarizeResult, *, root: Path | None = None) -> None:
    if result.slides is None:
        return
    for slide in result.slides.slides:
        path = Path(slide.image_path)
        if not path.is_absolute() and root is not None:
            path = root / path
        if not path.is_file():
            raise SummarizeContractError(f"slide frame is not a readable file: {path}")
        if path.stat().st_size < 8:
            raise SummarizeContractError(f"slide frame bytes are empty or truncated: {path}")
        header = path.read_bytes()[:8]
        if header[:8] != b"\x89PNG\r\n\x1a\n" and header[:2] != b"\xff\xd8":
            raise SummarizeContractError(f"slide frame is not a PNG or JPEG: {path}")


def validate_source_kind(result: SummarizeResult) -> None:
    kinds = []
    if result.slides is not None:
        kinds.append(result.slides.source_kind)
    if result.extract is not None:
        kinds.append(result.extract.source_kind)
    if len(set(kinds)) != 1:
        raise SummarizeContractError(f"slides/extract source kinds are not distinguishable: {kinds}")
    if kinds[0] not in {"youtube", "local", "url"}:
        raise SummarizeContractError(f"unknown source kind {kinds[0]!r}")


def compare_to_native(result: SummarizeResult, native: NativeIngestExpectation) -> list[str]:
    """Return capability gaps versus the documented native yt-dlp/FFmpeg path."""

    gaps: list[str] = []
    kind = result.slides.source_kind if result.slides else result.extract.source_kind  # type: ignore[union-attr]
    if kind != native.source_kind and not (kind == "url" and native.source_kind == "youtube"):
        gaps.append(f"source kind {kind} != native {native.source_kind}")
    if result.slides is not None:
        timestamps = [slide.timestamp for slide in result.slides.slides]
        if timestamps != native.frame_timestamps:
            gaps.append(
                f"frame timestamps {timestamps} differ from native expectation {native.frame_timestamps}"
            )
        if len(result.slides.slides) <= UPSTREAM_DEFAULT_SLIDES_MAX and not native.slides_capped_by_page_budget:
            # Not automatically a gap if the sample is short; record the upstream default cap.
            if len(native.frame_timestamps) > UPSTREAM_DEFAULT_SLIDES_MAX:
                gaps.append(
                    f"summarize default --slides-max {UPSTREAM_DEFAULT_SLIDES_MAX} would drop "
                    f"{len(native.frame_timestamps) - UPSTREAM_DEFAULT_SLIDES_MAX} native frames"
                )
    if result.extract is not None:
        origins = {segment.origin for segment in result.extract.segments}
        if native.caption_origin != "none" and native.caption_origin not in origins and origins != {native.caption_origin}:
            if native.caption_origin not in origins:
                gaps.append(
                    f"extract origins {sorted(origins)} do not include native {native.caption_origin}"
                )
    if native.download_count != 1:
        gaps.append("native expectation is a single download; summarize cache reuse was not live-verified")
    return gaps


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _source_from_command(command: Sequence[str]) -> str:
    """Extract the positional source after the ``--`` option separator."""

    try:
        separator = list(command).index("--")
    except ValueError as error:
        raise SummarizeContractError(
            "summarize command must include '--' before its source"
        ) from error
    sources = list(command)[separator + 1 :]
    if len(sources) != 1 or not sources[0]:
        raise SummarizeContractError(
            "summarize command must contain exactly one source after '--'"
        )
    return sources[0]


def run_summarize(
    command: Sequence[str],
    *,
    timeout: float = 120.0,
    runner: Runner = subprocess.run,
    cwd: Path | None = None,
    frame_root: Path | None = None,
    duration_seconds: float | None = None,
    source: str | None = None,
) -> SummarizeResult:
    """Run summarize without a shell. Non-zero exits never become ``ok: true``."""

    if not command or command[0] != "summarize":
        raise SummarizeContractError("adapter only executes the summarize argv vector")
    command_source = _source_from_command(command)
    if source is not None and source != command_source:
        raise SummarizeContractError(
            "explicit source does not match the summarize command source"
        )
    input_source = command_source
    completed = runner(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
    )
    if completed.returncode != 0:
        raise SummarizeFailedError(
            f"summarize exited {completed.returncode}: {(completed.stderr or completed.stdout)[:400]}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SummarizeContractError("summarize stdout is not JSON") from error
    if not isinstance(payload, dict):
        raise SummarizeContractError("summarize JSON must be an object")
    return parse_summarize_json(
        payload,
        exit_code=completed.returncode,
        source=input_source,
        frame_root=frame_root,
        duration_seconds=duration_seconds,
    )
