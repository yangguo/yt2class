"""ASR contract and optional WhisperX process adapter.

The adapter parses a small JSON contract and keeps model execution behind a
separate process.  WhisperX is intentionally not imported at module import
time, so ordinary installs and CI do not download a model.
"""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import subprocess
from threading import Event
from typing import Callable, Literal

from pydantic import Field, model_validator

from yt2class.adapters.process import (
    ProcessCancelled,
    ProcessError,
    ProcessTimedOut,
    ProcessUnavailable,
    run_process,
)
from yt2class.domain.common import Digest, Identifier, Seconds, StrictModel
from yt2class.domain.source import SourceInputError, content_sha256
from yt2class.domain.transcript import TranscriptWord
from yt2class.workers.whisperx_worker import build_whisperx_command


class ASRError(RuntimeError):
    """Raised when an ASR process or result is unusable."""


class ASRContractError(ASRError):
    """Raised when a worker returns malformed JSON or timestamps."""


class ASRCancelled(ASRError):
    """Raised when ASR is cancelled before or during execution."""


class ASRTimeout(ASRError):
    """Raised when an ASR worker exceeds its timeout."""


ASRAlignment = Literal["none", "sentence", "word"]
ASRSegmentAlignment = Literal["aligned", "unaligned", "partial"]
ASRStatus = Literal["complete", "degraded", "failed", "cancelled"]


class ASRRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier
    source_id: Identifier
    audio_path: Path
    language: str | None = Field(default=None, min_length=2, max_length=35)
    engine: str = Field(default="whisperx", min_length=1, max_length=64)
    model: str = Field(default="small", min_length=1, max_length=100)
    device: str = Field(default="cpu", min_length=1, max_length=32)
    align: bool = True
    diarize: bool = False
    offset_seconds: Seconds = 0.0
    timeout_seconds: float = Field(default=3600.0, gt=0, allow_inf_nan=False)


class ASRSegment(StrictModel):
    id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    text_original: str = Field(min_length=1, max_length=8000)
    language: str = Field(min_length=2, max_length=35)
    words: list[TranscriptWord] | None = None
    speaker_id: Identifier | None = None
    alignment_status: ASRSegmentAlignment
    quality_flags: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def check_range(self):
        if not self.start_seconds < self.end_seconds:
            raise ValueError("ASR segment range must be half-open")
        return self


class ASRResult(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Identifier
    source_id: Identifier
    engine: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=100)
    language: str = Field(min_length=2, max_length=35)
    device: str = Field(min_length=1, max_length=32)
    alignment: ASRAlignment
    diarization: bool
    offset_seconds: Seconds
    status: ASRStatus
    segments: list[ASRSegment] = Field(default_factory=list)
    raw_artifact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    audio_sha256: Digest | None = None
    parent_hash: Digest | None = None
    error: str | None = Field(default=None, max_length=400)


Runner = Callable[..., object]


def build_audio_extract_command(input_path: Path, output_path: Path) -> list[str]:
    """Build the deterministic mono 16 kHz PCM input expected by ASR."""

    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(Path(input_path)),
        "-map",
        "0:a:0?",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(Path(output_path)),
    ]


def build_asr_command(request: ASRRequest) -> list[str]:
    """Build the argv that crosses the optional WhisperX worker boundary."""

    return build_whisperx_command(
        request.audio_path,
        model=request.model,
        device=request.device,
        language=request.language,
        align=request.align,
        diarize=request.diarize,
        offset_seconds=request.offset_seconds,
    )


def extract_audio(
    input_path: Path,
    output_path: Path,
    *,
    runner: Runner = subprocess.run,
    timeout_seconds: float = 300.0,
    cancel_event: Event | None = None,
) -> Path:
    """Extract an ASR-ready audio artifact using an atomic temporary output."""

    input_path = Path(input_path)
    output_path = Path(output_path)
    if not input_path.is_file() or input_path.stat().st_size == 0:
        raise ASRError(f"ASR source media is missing or empty: {input_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
    temporary.unlink(missing_ok=True)
    command = build_audio_extract_command(input_path, temporary)
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
        temporary.unlink(missing_ok=True)
        raise ASRCancelled(str(error)) from error
    except ProcessTimedOut as error:
        temporary.unlink(missing_ok=True)
        raise ASRTimeout(str(error)) from error
    except (ProcessUnavailable, ProcessError, FileNotFoundError) as error:
        temporary.unlink(missing_ok=True)
        raise ASRError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        temporary.unlink(missing_ok=True)
        raise ASRTimeout(f"audio extraction timed out after {timeout_seconds:.1f}s") from error
    if cancel_event is not None and cancel_event.is_set():
        temporary.unlink(missing_ok=True)
        raise ASRCancelled("audio extraction cancelled")
    if int(getattr(completed, "returncode", 1)) != 0:
        temporary.unlink(missing_ok=True)
        detail = str(getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "audio extraction failed")
        raise ASRError(detail.strip())
    if not temporary.is_file() or temporary.stat().st_size == 0:
        temporary.unlink(missing_ok=True)
        raise ASRError("ffmpeg completed but did not produce ASR audio")
    temporary.replace(output_path)
    return output_path


def _as_float(value: object, *, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ASRContractError(f"ASR {label} must be numeric") from error
    if result != result or result in {float("inf"), float("-inf")}:
        raise ASRContractError(f"ASR {label} must be finite")
    return result


def _parse_words(raw_words: object, *, offset: float) -> tuple[list[TranscriptWord] | None, str, list[str]]:
    if not isinstance(raw_words, list) or not raw_words:
        return None, "unaligned", ["word-alignment-missing"]
    words: list[TranscriptWord] = []
    for raw in raw_words:
        if not isinstance(raw, dict) or raw.get("start") is None or raw.get("end") is None:
            return None, "unaligned", ["word-alignment-missing"]
        start = _as_float(raw.get("start"), label="word start") + offset
        end = _as_float(raw.get("end"), label="word end") + offset
        text = str(raw.get("word") or raw.get("text") or "").strip()
        if not text or not start < end:
            return None, "unaligned", ["word-alignment-missing"]
        try:
            words.append(
                TranscriptWord(
                    text=text,
                    start_seconds=start,
                    end_seconds=end,
                    aligned=True,
                )
            )
        except ValueError as error:
            raise ASRContractError(f"invalid ASR word range: {error}") from error
    return words, "aligned", []


def parse_asr_json(payload: object, request: ASRRequest) -> ASRResult:
    """Validate worker JSON and translate chunk-local times to source time."""

    if not isinstance(payload, dict):
        raise ASRContractError("ASR result must be a JSON object")
    raw_segments = payload.get("segments", [])
    if not isinstance(raw_segments, list):
        raise ASRContractError("ASR segments must be an array")
    language = str(payload.get("language") or request.language or "und")
    if len(language) < 2:
        language = "und"
    parsed: list[ASRSegment] = []
    alignment: ASRAlignment = "none"
    for index, raw in enumerate(raw_segments, start=1):
        if not isinstance(raw, dict):
            raise ASRContractError("ASR segment must be an object")
        start = _as_float(raw.get("start"), label="segment start") + request.offset_seconds
        end = _as_float(raw.get("end"), label="segment end") + request.offset_seconds
        if not start < end:
            raise ASRContractError("ASR segment range must be half-open")
        text = str(raw.get("text_original") or raw.get("text") or "").strip()
        if not text:
            raise ASRContractError("ASR segment text cannot be empty")
        words, segment_alignment, flags = _parse_words(
            raw.get("words"), offset=request.offset_seconds
        )
        if words is None:
            segment_alignment = "unaligned"
        elif segment_alignment == "aligned":
            alignment = "word"
        try:
            parsed.append(
                ASRSegment(
                    id=f"asr-{index:04d}",
                    start_seconds=start,
                    end_seconds=end,
                    text_original=text,
                    language=str(raw.get("language") or language),
                    words=words,
                    speaker_id=(str(raw["speaker"]) if raw.get("speaker") is not None else None),
                    alignment_status=segment_alignment,
                    quality_flags=flags,
                )
            )
        except ValueError as error:
            raise ASRContractError(f"invalid ASR segment: {error}") from error
    if parsed and alignment != "word":
        alignment = "sentence"
    raw_hash = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    status = str(payload.get("status") or "complete")
    if status not in {"complete", "degraded", "failed", "cancelled"}:
        status = "complete"
    return ASRResult(
        request_id=request.request_id,
        source_id=request.source_id,
        engine=str(payload.get("engine") or request.engine),
        model=str(payload.get("model") or request.model),
        language=language,
        device=str(payload.get("device") or request.device),
        alignment=alignment,
        diarization=bool(payload.get("diarization", request.diarize)),
        offset_seconds=request.offset_seconds,
        status=status,  # type: ignore[arg-type]
        segments=parsed,
        raw_artifact_hash=raw_hash,
        error=(str(payload["error"]) if payload.get("error") is not None else None),
    )


def run_asr(
    request: ASRRequest,
    *,
    runner: Runner = subprocess.run,
    cancel_event: Event | None = None,
) -> ASRResult:
    """Run the optional worker or a controlled test runner."""

    if cancel_event is not None and cancel_event.is_set():
        raise ASRCancelled("ASR cancelled before start")
    if not request.audio_path.is_file() or request.audio_path.stat().st_size == 0:
        raise ASRError(f"ASR audio is missing or empty: {request.audio_path}")
    command = build_asr_command(request)
    try:
        if runner is subprocess.run:
            completed = run_process(
                command,
                timeout_seconds=request.timeout_seconds,
                cancel_event=cancel_event,
            )
        else:
            completed = runner(
                command,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
                shell=False,
            )
    except ProcessCancelled as error:
        raise ASRCancelled(str(error)) from error
    except ProcessTimedOut as error:
        raise ASRTimeout(str(error)) from error
    except ProcessUnavailable as error:
        raise ASRError("ASR worker executable is unavailable") from error
    except ProcessError as error:
        raise ASRError(str(error)) from error
    except subprocess.TimeoutExpired as error:
        raise ASRTimeout(f"ASR timed out after {request.timeout_seconds:.1f}s") from error
    except TimeoutError as error:
        raise ASRTimeout(str(error)) from error
    except FileNotFoundError as error:
        raise ASRError("ASR worker executable is unavailable") from error
    if cancel_event is not None and cancel_event.is_set():
        raise ASRCancelled("ASR cancelled")
    returncode = int(getattr(completed, "returncode", 1))
    stdout = str(getattr(completed, "stdout", "") or "")
    stderr = str(getattr(completed, "stderr", "") or "")
    if returncode != 0:
        raise ASRError(f"ASR worker failed: {(stderr or stdout or 'unknown failure').strip()}")
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ASRContractError("ASR worker returned invalid JSON") from error
    result = parse_asr_json(payload, request)
    try:
        audio_hash = content_sha256(request.audio_path)
    except (OSError, SourceInputError) as error:
        raise ASRError(f"cannot hash ASR audio: {request.audio_path}") from error
    return result.model_copy(update={"audio_sha256": audio_hash})


__all__ = [
    "ASRCancelled",
    "ASRContractError",
    "ASRError",
    "ASRRequest",
    "ASRResult",
    "ASRSegment",
    "ASRTimeout",
    "build_audio_extract_command",
    "build_asr_command",
    "extract_audio",
    "parse_asr_json",
    "run_asr",
]
