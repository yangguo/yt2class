"""Apply minimal ASR/caption repairs supported by on-screen OCR evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from yt2class.domain.transcript import TranscriptDocument, TranscriptSegment
from yt2class.domain.visual import FrameOccurrence, VisualCatalogue

_VISUAL_CORRECTED_FLAG = "visual-asr-corrected"


@dataclass(frozen=True)
class HeadwordRule:
    """OCR must match ocr_pattern; ASR variants may be replaced with the OCR span."""

    ocr_pattern: re.Pattern[str]
    asr_variants: tuple[str, ...]


@dataclass(frozen=True)
class VisualHeadword:
    canonical: str
    center_seconds: float
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class TranscriptVisualCorrection:
    segment_id: str
    before: str
    after: str
    visual_form: str
    visual_evidence_ids: list[str]


_DEFAULT_RULES: tuple[HeadwordRule, ...] = (
    HeadwordRule(
        ocr_pattern=re.compile(r"～?につき"),
        asr_variants=("2月", "に月", "兄月", "ニ月", "～2月", "〜2月"),
    ),
)


def _occurrence_seconds(occurrence: FrameOccurrence) -> float:
    if occurrence.actual_source_seconds is not None:
        return float(occurrence.actual_source_seconds)
    if occurrence.timestamp_seconds is not None:
        return float(occurrence.timestamp_seconds)
    return float(occurrence.requested_seconds)


def _segments_overlap(
    seg_start: float,
    seg_end: float,
    win_start: float,
    win_end: float,
) -> bool:
    return seg_end > win_start and seg_start < win_end


def _collect_visual_headwords(
    visual: VisualCatalogue,
    rules: Iterable[HeadwordRule],
) -> list[VisualHeadword]:
    occurrences = {item.id: item for item in visual.occurrences}
    found: list[VisualHeadword] = []
    for region in visual.ocr_regions:
        parent = occurrences.get(region.parent_occurrence_id)
        if parent is None:
            continue
        center = _occurrence_seconds(parent)
        for rule in rules:
            for match in rule.ocr_pattern.finditer(region.text):
                found.append(
                    VisualHeadword(
                        canonical=match.group(0),
                        center_seconds=center,
                        evidence_ids=(region.id, region.parent_occurrence_id),
                    )
                )
    return found


def _apply_variants(text: str, visual_form: str, variants: tuple[str, ...]) -> str:
    updated = text
    for variant in sorted(variants, key=len, reverse=True):
        if variant in updated:
            updated = updated.replace(variant, visual_form)
    return updated


def correct_transcript_from_visual(
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    *,
    window_seconds: float = 45.0,
    rules: Iterable[HeadwordRule] = _DEFAULT_RULES,
) -> tuple[TranscriptDocument, list[TranscriptVisualCorrection]]:
    """Rewrite nearby transcript tokens when OCR shows a conflicting grammar headword."""

    rules_tuple = tuple(rules)
    headwords = _collect_visual_headwords(visual, rules_tuple)
    if not headwords or not transcript.segments:
        return transcript, []

    half = max(1.0, window_seconds / 2.0)
    corrections: list[TranscriptVisualCorrection] = []
    updated_segments: list[TranscriptSegment] = []

    for segment in transcript.segments:
        text = segment.text_original
        flags = list(segment.quality_flags)
        for headword in headwords:
            win_start = headword.center_seconds - half
            win_end = headword.center_seconds + half
            if not _segments_overlap(
                segment.start_seconds,
                segment.end_seconds,
                win_start,
                win_end,
            ):
                continue
            for rule in rules_tuple:
                if not rule.ocr_pattern.search(headword.canonical):
                    continue
                replaced = _apply_variants(text, headword.canonical, rule.asr_variants)
                if replaced != text:
                    corrections.append(
                        TranscriptVisualCorrection(
                            segment_id=segment.id,
                            before=text,
                            after=replaced,
                            visual_form=headword.canonical,
                            visual_evidence_ids=list(headword.evidence_ids),
                        )
                    )
                    text = replaced
                    if _VISUAL_CORRECTED_FLAG not in flags:
                        flags.append(_VISUAL_CORRECTED_FLAG)
        if text == segment.text_original and flags == list(segment.quality_flags):
            updated_segments.append(segment)
        else:
            updated_segments.append(
                segment.model_copy(
                    update={"text_original": text, "quality_flags": flags[:20]}
                )
            )

    if not corrections:
        return transcript, []
    return transcript.model_copy(update={"segments": updated_segments}), corrections
