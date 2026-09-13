"""Shared helpers for outline/segment LLM requests. No provider HTTP."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from yt2class.adapters.providers.base import ModelRequest, ProviderRole
from yt2class.domain.resolvers import evidence_universe
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue, is_accepted_visual_occurrence

PROMPT_DIR = Path(__file__).resolve().parents[1] / "prompts"
CHARS_PER_TOKEN = 4
IMAGE_TOKEN_ESTIMATE = 256
OUTPUT_RESERVE_TOKENS = 512
PATH_HINT = (
    r"(?:^|[\s\"'])(?:(?:[A-Za-z]:\\|\\\\|/|\./|\.\./)[^\s\"']+\.(?:jpg|jpeg|png|webp|gif|mp4|mkv|mov|webm))"
)
NEGATION_MARKERS = (
    "不是",
    "不会",
    "不能",
    "不要",
    "并非",
    "并未",
    "没有",
    "从未",
    "绝不",
    " not ",
    "n't",
    "never",
    "no ",
)
UNIT_PATTERN_HINTS = (
    "秒",
    "分钟",
    "小时",
    "度",
    "cm",
    "mm",
    "kg",
    "m/s",
    "%",
    "％",
)


def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    return path.read_text(encoding="utf-8")


def payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def estimate_tokens(text: str, *, image_count: int = 0) -> int:
    return max(1, (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN) + (
        max(0, image_count) * IMAGE_TOKEN_ESTIMATE
    )


def allowed_evidence_ids(
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
) -> set[str]:
    return evidence_universe(transcript, visual)


def occurrence_timestamp(occurrence: object) -> float | None:
    actual = getattr(occurrence, "actual_source_seconds", None)
    if actual is not None:
        return float(actual)
    stamped = getattr(occurrence, "timestamp_seconds", None)
    if stamped is not None:
        return float(stamped)
    requested = getattr(occurrence, "requested_seconds", None)
    return None if requested is None else float(requested)


def accepted_occurrences(visual: VisualCatalogue) -> list[object]:
    assets = {asset.id: asset for asset in visual.assets}
    return [
        occurrence
        for occurrence in visual.occurrences
        if is_accepted_visual_occurrence(occurrence, assets)
    ]


def evidence_in_range(
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    start: float,
    end: float,
) -> list[str]:
    ids: list[str] = []
    for segment in transcript.segments:
        if segment.end_seconds > start and segment.start_seconds < end:
            ids.append(segment.id)
    for occurrence in accepted_occurrences(visual):
        timestamp = occurrence_timestamp(occurrence)
        if timestamp is not None and start <= timestamp < end:
            ids.append(occurrence.id)
    ocr_by_parent = {region.parent_occurrence_id: region.id for region in visual.ocr_regions}
    for occurrence_id, region_id in ocr_by_parent.items():
        if occurrence_id in ids and region_id not in ids:
            ids.append(region_id)
    return ids


def transcript_in_range(
    transcript: TranscriptDocument,
    start: float,
    end: float,
) -> list[dict[str, Any]]:
    excerpts: list[dict[str, Any]] = []
    for segment in transcript.segments:
        if segment.end_seconds > start and segment.start_seconds < end:
            excerpts.append(
                {
                    "id": segment.id,
                    "start_seconds": segment.start_seconds,
                    "end_seconds": segment.end_seconds,
                    "text_original": segment.text_original,
                }
            )
    return excerpts


def frames_in_range(visual: VisualCatalogue, start: float, end: float) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    ocr_by_parent: dict[str, list[str]] = {}
    for region in visual.ocr_regions:
        ocr_by_parent.setdefault(region.parent_occurrence_id, []).append(region.text)
    for occurrence in accepted_occurrences(visual):
        timestamp = occurrence_timestamp(occurrence)
        if timestamp is None or not start <= timestamp < end:
            continue
        frames.append(
            {
                "id": occurrence.id,
                "timestamp_seconds": timestamp,
                "ocr_text": " ".join(ocr_by_parent.get(occurrence.id, [])),
            }
        )
    frames.sort(key=lambda item: (item["timestamp_seconds"], item["id"]))
    return frames


def contains_path_literal(value: str) -> bool:
    import re

    if "/" in value or "\\" in value:
        if re.search(PATH_HINT, value, flags=re.IGNORECASE):
            return True
        lowered = value.lower()
        if any(lowered.endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4")):
            return True
    return False


def text_has_negation(text: str) -> bool:
    lowered = f" {text.lower()} "
    return any(marker in text or marker in lowered for marker in NEGATION_MARKERS)


def text_has_units(text: str) -> bool:
    import re

    if re.search(r"\d+(?:\.\d+)?\s*(?:秒|分钟|小时|度|cm|mm|kg|m/s|%|％)", text, flags=re.I):
        return True
    return any(hint in text for hint in UNIT_PATTERN_HINTS if hint.isascii() is False)


def join_texts(items: Iterable[str]) -> str:
    return "\n".join(item for item in items if item)


def model_request(
    *,
    request_id: str,
    role: ProviderRole,
    payload: dict[str, Any],
    image_count: int = 0,
    video_seconds: float = 0.0,
    output_tokens: int = OUTPUT_RESERVE_TOKENS,
) -> ModelRequest:
    modalities = ["text"]
    if image_count:
        modalities.append("image")
    if video_seconds:
        modalities.append("video")
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return ModelRequest(
        request_id=request_id,
        role=role,
        modalities=modalities,
        estimated_input_tokens=estimate_tokens(encoded, image_count=image_count),
        estimated_output_tokens=output_tokens,
        image_count=image_count,
        video_seconds=video_seconds,
        require_structured_output=True,
        payload_digest=payload_digest(payload),
    )
