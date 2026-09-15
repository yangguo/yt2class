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
_TRANSCRIPT_CALENDAR_TSUKI = re.compile(
    r"(来月|今月|先月|毎月).{0,12}?2月|2月の(予定|休み|行事|カレンダー)|2月です"
)
_GRAMMAR_TSUKI_CONTEXT = re.compile(
    r"文法|用法|助詞|意味|例文|説明|について|につき|表現|～|〜"
)

_TSUKI_ASR_VARIANTS: tuple[str, ...] = (
    "～2つき",
    "〜2つき",
    "２つき",
    "2つき",
    "ニつき",
    "二つき",
    "2ツキ",
    "ニツキ",
    "～2月",
    "〜2月",
    "2月",
    "に月",
    "兄月",
    "ニ月",
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


def _lesson_canonical_tsuki(headwords: list[VisualHeadword]) -> str | None:
    if not headwords:
        return None
    priority = {"～につき": 4, "〜につき": 4, "につき": 3, "～つき": 2, "〜つき": 2}
    best = max(headwords, key=lambda h: (priority.get(h.canonical, 1), -h.center_seconds))
    return best.canonical


def _text_has_tsuki_asr_variant(text: str) -> bool:
    return any(variant in text for variant in _TSUKI_ASR_VARIANTS)


def _segment_eligible_for_lesson_tsuki_correction(text: str) -> bool:
    """True when ASR likely misread につき (not a calendar-month mention)."""

    if not _text_has_tsuki_asr_variant(text):
        return False
    if _TRANSCRIPT_CALENDAR_TSUKI.search(text):
        return False
    return bool(_GRAMMAR_TSUKI_CONTEXT.search(text))


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
    lesson_canonical = _lesson_canonical_tsuki(headwords)
    lesson_evidence = tuple(
        dict.fromkeys(eid for hw in headwords for eid in hw.evidence_ids)
    )
    corrections: list[TranscriptVisualCorrection] = []
    updated_segments: list[TranscriptSegment] = []

    for segment in transcript.segments:
        text = segment.text_original
        flags = list(segment.quality_flags)
        applied_canonical: str | None = None
        applied_evidence: tuple[str, ...] = ()
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
                applied_canonical = headword.canonical
                applied_evidence = headword.evidence_ids
                text = replaced
        if (
            lesson_canonical is not None
            and _segment_eligible_for_lesson_tsuki_correction(text)
        ):
            replaced = _apply_variants(text, lesson_canonical, _TSUKI_ASR_VARIANTS)
            if replaced != text:
                applied_canonical = lesson_canonical
                applied_evidence = lesson_evidence
                text = replaced
        if applied_canonical is not None:
            corrections.append(
                TranscriptVisualCorrection(
                    segment_id=segment.id,
                    before=segment.text_original,
                    after=text,
                    visual_form=applied_canonical,
                    visual_evidence_ids=list(applied_evidence),
                )
            )
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
