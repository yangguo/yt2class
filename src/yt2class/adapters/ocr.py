"""Optional OCR adapter with explicit unavailable/degraded states."""

from __future__ import annotations

from enum import Enum
import json
from pathlib import Path
import subprocess
from threading import Event
from typing import Callable

from pydantic import Field

from yt2class.adapters.process import (
    ProcessCancelled,
    ProcessError,
    ProcessTimedOut,
    ProcessUnavailable,
    run_process,
)
from yt2class.domain.common import Identifier, StrictModel, UnitInterval
from yt2class.domain.visual import BoundingBox


class OCRContractError(ValueError):
    """Raised when OCR output cannot be bound to a frame occurrence."""


class OCRCancelled(RuntimeError):
    """Raised when OCR is cancelled by the caller."""


class OCRStatus(str, Enum):
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


class OCRRegionResult(StrictModel):
    id: Identifier
    asset_id: Identifier
    parent_occurrence_id: Identifier
    bbox: BoundingBox
    text: str = Field(min_length=1, max_length=4000)
    engine: str = Field(min_length=1, max_length=64)
    confidence: UnitInterval


class OCRResult(StrictModel):
    asset_id: Identifier
    occurrence_id: Identifier
    engine: str = Field(min_length=1, max_length=64)
    status: OCRStatus
    regions: list[OCRRegionResult] = Field(default_factory=list)
    error: str | None = Field(default=None, max_length=400)


Runner = Callable[..., object]


def build_ocr_command(image_path: Path, *, engine: str = "tesseract") -> list[str]:
    if engine == "tesseract":
        return ["tesseract", str(Path(image_path)), "stdout", "--psm", "6", "tsv"]
    # Custom engines are allowed behind the same argv/JSON contract so tests
    # and deployments can inject a local OCR service without changing stages.
    return [engine, str(Path(image_path))]


def _parse_json_result(
    payload: object,
    *,
    asset_id: str,
    occurrence_id: str,
    default_engine: str,
) -> OCRResult:
    if not isinstance(payload, dict):
        raise OCRContractError("OCR result must be a JSON object")
    raw_regions = payload.get("regions", [])
    if not isinstance(raw_regions, list):
        raise OCRContractError("OCR regions must be an array")
    regions: list[OCRRegionResult] = []
    for index, raw in enumerate(raw_regions, start=1):
        if not isinstance(raw, dict):
            raise OCRContractError("OCR region must be an object")
        try:
            regions.append(
                OCRRegionResult(
                    id=f"ocr-{occurrence_id}-{index:04d}",
                    asset_id=asset_id,
                    parent_occurrence_id=occurrence_id,
                    bbox=raw.get("bbox"),
                    text=str(raw.get("text") or "").strip(),
                    engine=str(raw.get("engine") or payload.get("engine") or default_engine),
                    confidence=raw.get("confidence", 0.0),
                )
            )
        except ValueError as error:
            raise OCRContractError(f"invalid OCR bbox or region: {error}") from error
    return OCRResult(
        asset_id=asset_id,
        occurrence_id=occurrence_id,
        engine=str(payload.get("engine") or default_engine),
        status=OCRStatus.COMPLETE,
        regions=regions,
    )


def _parse_tesseract_tsv(
    text: str,
    *,
    asset_id: str,
    occurrence_id: str,
) -> OCRResult:
    """Parse Tesseract's TSV output without treating its confidence as truth."""

    lines = text.splitlines()
    if not lines:
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine="tesseract",
            status=OCRStatus.COMPLETE,
            regions=[],
        )
    regions: list[OCRRegionResult] = []
    for index, line in enumerate(lines[1:], start=1):
        fields = line.split("\t")
        if len(fields) < 12:
            continue
        value = fields[11].strip()
        if not value:
            continue
        try:
            left, top, width, height = (int(fields[position]) for position in (6, 7, 8, 9))
            confidence = max(0.0, min(1.0, float(fields[10]) / 100.0))
            region = OCRRegionResult(
                id=f"ocr-{occurrence_id}-{index:04d}",
                asset_id=asset_id,
                parent_occurrence_id=occurrence_id,
                bbox={"x": left, "y": top, "width": width, "height": height},
                text=value,
                engine="tesseract",
                confidence=confidence,
            )
        except (TypeError, ValueError) as error:
            raise OCRContractError(f"invalid Tesseract TSV region: {error}") from error
        regions.append(region)
    return OCRResult(
        asset_id=asset_id,
        occurrence_id=occurrence_id,
        engine="tesseract",
        status=OCRStatus.COMPLETE,
        regions=regions,
    )


def run_ocr(
    image_path: Path,
    *,
    asset_id: str,
    occurrence_id: str,
    engine: str = "none",
    runner: Runner = subprocess.run,
    timeout_seconds: float = 60.0,
    cancel_event: Event | None = None,
) -> OCRResult:
    """Run optional OCR, returning unavailable/failed status without fake text."""

    if cancel_event is not None and cancel_event.is_set():
        raise OCRCancelled("OCR cancelled before start")
    if engine in {"none", "", "unavailable"}:
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine="none",
            status=OCRStatus.UNAVAILABLE,
            error="OCR engine is not configured",
        )
    if not Path(image_path).is_file() or Path(image_path).stat().st_size == 0:
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine=engine,
            status=OCRStatus.FAILED,
            error=f"OCR image is missing or empty: {image_path}",
        )
    command = build_ocr_command(image_path, engine=engine)
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
        raise OCRCancelled(str(error)) from error
    except (ProcessTimedOut, ProcessUnavailable, ProcessError, FileNotFoundError) as error:
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine=engine,
            status=OCRStatus.FAILED,
            error=str(error),
        )
    except subprocess.TimeoutExpired as error:
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine=engine,
            status=OCRStatus.FAILED,
            error=f"OCR timed out after {timeout_seconds:.1f}s",
        )
    if cancel_event is not None and cancel_event.is_set():
        raise OCRCancelled("OCR cancelled")
    if int(getattr(completed, "returncode", 1)) != 0:
        detail = str(getattr(completed, "stderr", "") or getattr(completed, "stdout", "") or "OCR failed")
        return OCRResult(
            asset_id=asset_id,
            occurrence_id=occurrence_id,
            engine=engine,
            status=OCRStatus.FAILED,
            error=detail.strip(),
        )
    stdout = str(getattr(completed, "stdout", "") or "")
    if engine == "tesseract":
        return _parse_tesseract_tsv(
            stdout,
            asset_id=asset_id,
            occurrence_id=occurrence_id,
        )
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise OCRContractError("OCR engine returned invalid JSON") from error
    return _parse_json_result(
        payload,
        asset_id=asset_id,
        occurrence_id=occurrence_id,
        default_engine=engine,
    )


def ocr_density(text: str, *, width: int, height: int) -> float:
    """Return a bounded, deterministic text-density signal for frame quality."""

    return min(1.0, len(text.strip()) / max(1.0, width * height / 1000.0))


__all__ = [
    "OCRCancelled",
    "OCRContractError",
    "OCRRegionResult",
    "OCRResult",
    "OCRStatus",
    "build_ocr_command",
    "ocr_density",
    "run_ocr",
]
