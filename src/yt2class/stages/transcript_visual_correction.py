"""Apply minimal ASR/caption repairs supported by on-screen OCR evidence."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from yt2class.domain.transcript import TranscriptDocument, TranscriptSegment
from yt2class.domain.visual import FrameOccurrence, VisualCatalogue

_VISUAL_CORRECTED_FLAG = "visual-asr-corrected"

_TSUKI_FULL = re.compile(r"(～|〜)につき")
_TSUKI_NI = re.compile(r"につき")
_TSUKI_WAVE_ONLY = re.compile(r"(～|〜)つき")
_TSUKI_FRAGMENT = re.compile(r"(?<![一-龥])つき(?![一-龥ぁ-ん])")
_CALENDAR_MONTH = re.compile(r"\d+月")

_TSUKI_ASR_VARIANTS: tuple[str, ...] = (
    "2月",
    "に月",
    "兄月",
    "ニ月",
    "～2月",
    "〜2月",
)


@dataclass(frozen=True)
class HeadwordRule:
    """Optional generic rule hook (tsuki uses dedicated OCR logic)."""

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
        asr_variants=_TSUKI_ASR_VARIANTS,
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


def _ocr_has_tsuki_signal(ocr_text: str) -> bool:
    return bool(
        _TSUKI_FULL.search(ocr_text)
        or _TSUKI_NI.search(ocr_text)
        or _TSUKI_WAVE_ONLY.search(ocr_text)
        or _TSUKI_FRAGMENT.search(ocr_text)
    )


def _ocr_blocks_tsuki_correction(ocr_text: str) -> bool:
    """True when the board shows a calendar month, not a grammar につき headword."""

    if _ocr_has_tsuki_signal(ocr_text):
        return False
    return bool(_CALENDAR_MONTH.search(ocr_text))


def _best_tsuki_canonical(ocr_text: str) -> str | None:
    """Pick the best grammar headword form supported by OCR (fragment-tolerant)."""

    if _ocr_blocks_tsuki_correction(ocr_text):
        return None
    match = _TSUKI_FULL.search(ocr_text)
    if match:
        return match.group(0)
    if _TSUKI_NI.search(ocr_text):
        return "につき"
    match = _TSUKI_WAVE_ONLY.search(ocr_text)
    if match:
        wave = match.group(1)
        return f"{wave}につき"
    if _TSUKI_FRAGMENT.search(ocr_text):
        return "につき"
    return None


def _collect_tsuki_headwords(visual: VisualCatalogue) -> list[VisualHeadword]:
    occurrences = {item.id: item for item in visual.occurrences}
    found: list[VisualHeadword] = []
    for region in visual.ocr_regions:
        canonical = _best_tsuki_canonical(region.text)
        if canonical is None:
            continue
        parent = occurrences.get(region.parent_occurrence_id)
        if parent is None:
            continue
        found.append(
            VisualHeadword(
                canonical=canonical,
                center_seconds=_occurrence_seconds(parent),
                evidence_ids=(region.id, region.parent_occurrence_id),
            )
        )
    return found


def _collect_visual_headwords(visual: VisualCatalogue) -> list[VisualHeadword]:
    return _collect_tsuki_headwords(visual)


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

    del rules
    headwords = _collect_visual_headwords(visual)
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
            replaced = _apply_variants(text, headword.canonical, _TSUKI_ASR_VARIANTS)
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
