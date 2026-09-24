"""Global editor: deterministic coverage scoring, then LLM titles/order/copy."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from threading import Event
from typing import Any, Iterable

from pydantic import ValidationError

from yt2class.adapters.providers.base import Provider
from yt2class.domain.course_map import CourseMap, Topic
from yt2class.domain.editorial import (
    DeckOrder,
    EditorialPlan,
    Omission,
    PageIntent,
    PageLayout,
    PageQualityLabel,
    relabel_page,
)
from yt2class.domain.knowledge import KnowledgeDocument, KnowledgeUnit
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue, is_accepted_visual_occurrence
from yt2class.stages.llm_util import (
    attach_provider_payload,
    contains_path_literal,
    load_prompt,
    model_request,
)
from yt2class.stages.grammar_sense import (
    CONNECTIVE_HEADING,
    is_connective_topic,
    is_fixed_sense_heading,
    is_fixed_sense_summary_line,
    is_grammar_connective_topic,
    is_tsuki_grammar_lesson,
    iter_rate_sentences,
    learner_page_title,
    representative_rate_snippets,
    pick_summary_unit,
    pick_summary_unit_for_topic,
    sense_heading,
    sense_ordinal,
    sense_topic_ids,
    summary_bullet_for_sense,
    text_is_rate_only,
    text_is_rate_proportion,
    topic_for_connective,
    topic_for_sense_ordinal,
    unit_is_connective_attachment,
)
from yt2class.stages.student_copy import contains_student_meta, sanitize_student_copy

KIND_IMPORTANCE = {
    "concept": 1.0,
    "warning": 0.95,
    "procedure": 0.92,
    "comparison": 0.88,
    "example": 0.84,
    "recap": 0.18,
}

_MAX_EXAMPLES_PER_TOPIC = 2
_MAX_CAUSE_CONTENT_PAGES = 2
_MAX_ABOUT_CONTENT_PAGES = 1
_CONVERSATION_RE = re.compile(
    r"会話|对话|對話|會話|ネットカフェ|网咖|網咖|conversation|dialogue",
    re.I,
)
_PEDAGOGICAL_CONNECTIVE_RE = re.compile(r"接续|接続|连接|名詞|名词|数量詞|数量词")
_ENGLISH_INTRO_RE = re.compile(
    r"(?i)\b(?:introduction|intro|overview|what\s+goes\s+before)\b"
)


class EditorContractError(ValueError):
    """Structured editor output failed the EditorialPlan contract."""


@dataclass
class PageCandidate:
    id: str
    unit_id: str
    topic_id: str
    kind: str
    claim_ids: list[str]
    frame_ids: list[str]
    layout: PageLayout
    start_seconds: float
    end_seconds: float
    score: float
    required_prerequisite: bool
    selection_reason: str
    title: str
    notes: str
    body_points: list[str] = field(default_factory=list)
    representative_example: bool = False


def _unit_by_id(knowledge: KnowledgeDocument, unit_id: str) -> KnowledgeUnit | None:
    return next((unit for unit in knowledge.units if unit.id == unit_id), None)


def _unit_for_claim_ids(
    knowledge: KnowledgeDocument,
    claim_ids: list[str],
) -> KnowledgeUnit | None:
    wanted = set(claim_ids)
    if not wanted:
        return None
    return next(
        (
            unit
            for unit in knowledge.units
            if any(claim.id in wanted for claim in unit.claims)
        ),
        None,
    )


def _candidate_is_connective(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> bool:
    if not is_tsuki_grammar_lesson(course_map, knowledge):
        return False
    unit = _unit_by_id(knowledge, item.unit_id)
    if unit is None:
        return False
    topic = _topic_by_id(item.topic_id, course_map, knowledge)
    return unit_is_connective_attachment(unit, topic)


def _row_is_rate_only(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
) -> bool:
    unit = _unit_by_id(knowledge, item.unit_id)
    text = _unit_display_text(unit) if unit is not None else item.notes
    return text_is_rate_only(text)


def _row_has_rate_snippet(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
) -> bool:
    unit = _unit_by_id(knowledge, item.unit_id)
    text = _unit_display_text(unit) if unit is not None else item.notes
    return bool(representative_rate_snippets([text]))


def _candidate_ordinal(
    item: PageCandidate,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> int | None:
    """Sense slot for ordering. Proportion copy is 用法二 even on a cause topic."""

    if _candidate_is_connective(item, knowledge, course_map):
        return None
    if _row_is_rate_only(item, knowledge):
        return 2
    return sense_ordinal(item.topic_id, course_map, knowledge=knowledge)


def _is_usage_cause_page(
    item: PageCandidate,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> bool:
    """用法一 page that is not an attachment gloss or a proportion example."""

    if _candidate_is_connective(item, knowledge, course_map):
        return False
    if _row_is_rate_only(item, knowledge):
        return False
    return sense_ordinal(item.topic_id, course_map, knowledge=knowledge) == 1


def _is_usage_about_page(
    item: PageCandidate,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> bool:
    if _candidate_is_connective(item, knowledge, course_map) or _row_is_rate_only(item, knowledge):
        return False
    return sense_ordinal(item.topic_id, course_map, knowledge=knowledge) == 3


def prerequisite_parents(knowledge: KnowledgeDocument) -> dict[str, set[str]]:
    """Map unit_id -> unit_ids that must appear before it."""

    parents: dict[str, set[str]] = {unit.id: set() for unit in knowledge.units}
    claim_owner = {claim.id: unit.id for unit in knowledge.units for claim in unit.claims}
    for unit in knowledge.units:
        for relation in unit.relations:
            if relation.kind not in {"prerequisite", "step_before"}:
                continue
            src = claim_owner.get(relation.from_id, relation.from_id)
            dst = claim_owner.get(relation.to_id, relation.to_id)
            if src in parents and dst in parents and src != dst:
                parents[dst].add(src)
    return parents


def _unit_display_text(unit: KnowledgeUnit) -> str:
    return " ".join(claim.text for claim in unit.claims)


def _meta_filler_penalty(unit: KnowledgeUnit) -> float:
    if contains_student_meta(_unit_display_text(unit)):
        return 2.5
    if unit.kind == "recap":
        return 1.0
    return 0.0


def _score_unit(unit: KnowledgeUnit) -> tuple[float, str]:
    importance = KIND_IMPORTANCE.get(unit.kind, 0.5)
    evidence = min(1.0, sum(len(claim.evidence_ids) for claim in unit.claims) / 3.0)
    topic_gain = 1.0
    new_info = 1.0
    redundancy = 0.0
    meta_penalty = _meta_filler_penalty(unit)
    score = (
        0.35 * topic_gain
        + 0.25 * importance
        + 0.20 * evidence
        + 0.20 * new_info
        - 0.30 * redundancy
        - meta_penalty
    )
    reason = (
        f"coverage={topic_gain:.2f} importance={importance:.2f} "
        f"evidence={evidence:.2f} novelty={new_info:.2f} meta_penalty={meta_penalty:.2f}"
    )
    return score, reason


def _occurrence_time(occurrence: object) -> float:
    actual = getattr(occurrence, "actual_source_seconds", None)
    if actual is not None:
        return float(actual)
    stamped = getattr(occurrence, "timestamp_seconds", None)
    if stamped is not None:
        return float(stamped)
    return float(getattr(occurrence, "requested_seconds", 0.0))


def _frame_score(unit: KnowledgeUnit, occurrence: object, candidate: object | None) -> float:
    relevance = float(getattr(candidate, "relevance", 0.6))
    legibility = float(getattr(candidate, "legibility", getattr(occurrence.quality, "sharpness", 0.5)))
    quality = getattr(occurrence, "quality", None)
    complete = 0.5
    if quality is not None:
        complete = 0.5 * float(quality.ocr_density) + 0.5 * float(quality.sharpness)
    mid = (unit.start_seconds + unit.end_seconds) / 2
    span = max(1.0, unit.end_seconds - unit.start_seconds)
    represent = 1.0 - min(1.0, abs(_occurrence_time(occurrence) - mid) / span)
    return 0.45 * relevance + 0.30 * legibility + 0.15 * complete + 0.10 * represent


def _frame_ocr_text(frame_id: str, visual: VisualCatalogue) -> str:
    return "\n".join(
        region.text
        for region in visual.ocr_regions
        if region.parent_occurrence_id == frame_id
    )


def _frame_context_text(
    frame_id: str,
    visual: VisualCatalogue,
    transcript: TranscriptDocument | None,
) -> str:
    parts = [_frame_ocr_text(frame_id, visual)]
    occurrence = next((item for item in visual.occurrences if item.id == frame_id), None)
    if occurrence is not None and transcript is not None:
        stamp = _occurrence_time(occurrence)
        for segment in transcript.segments:
            if segment.start_seconds - 1 <= stamp <= segment.end_seconds + 1:
                parts.append(segment.text_original)
    return "\n".join(part for part in parts if part.strip())


def _frame_is_conversation(
    frame_id: str,
    visual: VisualCatalogue,
    transcript: TranscriptDocument | None,
) -> bool:
    return _CONVERSATION_RE.search(_frame_context_text(frame_id, visual, transcript)) is not None


def _frame_shows_rate(
    frame_id: str,
    visual: VisualCatalogue,
    transcript: TranscriptDocument | None,
) -> bool:
    """A 例文 board or nearby caption that would show a proportion line."""

    ocr = _frame_ocr_text(frame_id, visual)
    if any(text_is_rate_proportion(sentence) for sentence in iter_rate_sentences(ocr)):
        return True
    if text_is_rate_proportion(ocr):
        return True
    mixed_board = "例文" in ocr or len(re.findall(r"につき|つき", ocr)) >= 2
    if not mixed_board or transcript is None:
        return False
    occurrence = next((item for item in visual.occurrences if item.id == frame_id), None)
    if occurrence is None:
        return False
    stamp = _occurrence_time(occurrence)
    for segment in transcript.segments:
        midpoint = (segment.start_seconds + segment.end_seconds) / 2
        if abs(midpoint - stamp) > 45:
            continue
        if iter_rate_sentences(segment.text_original):
            return True
    return False


def _frame_is_pedagogical_connective(
    frame_id: str,
    visual: VisualCatalogue,
    transcript: TranscriptDocument | None,
) -> bool:
    text = _frame_context_text(frame_id, visual, transcript)
    if _CONVERSATION_RE.search(text):
        return False
    if _frame_shows_rate(frame_id, visual, transcript):
        return False
    return _PEDAGOGICAL_CONNECTIVE_RE.search(text) is not None


def _clean_claim_frame_ids(unit: KnowledgeUnit, occurrences: set[str]) -> set[str]:
    """Frames cited by learner sentences, not alignment notes."""

    cited: set[str] = set()
    for claim in unit.claims:
        cleaned = sanitize_student_copy(claim.text)
        if not cleaned.strip():
            continue
        if contains_student_meta(claim.text) and not cleaned.strip():
            continue
        for evidence_id in claim.evidence_ids:
            if evidence_id in occurrences:
                cited.add(evidence_id)
    return cited


def assign_frames(
    unit: KnowledgeUnit,
    visual: VisualCatalogue,
    *,
    used_frames: set[str],
    used_assets: set[str],
    role: str = "other",
    transcript: TranscriptDocument | None = None,
) -> tuple[list[str], PageLayout]:
    assets = {asset.id: asset for asset in visual.assets}
    occurrences = {
        item.id: item
        for item in visual.occurrences
        if is_accepted_visual_occurrence(item, assets)
    }
    by_candidate = {item.frame_id: item for item in unit.visual_candidates}
    cited = [item for claim in unit.claims for item in claim.evidence_ids if item in occurrences]
    pool = list(dict.fromkeys([*by_candidate, *cited]))
    clean_ids = _clean_claim_frame_ids(unit, set(occurrences))
    scored: list[tuple[float, str]] = []
    for frame_id in pool:
        occurrence = occurrences.get(frame_id)
        if occurrence is None:
            continue
        penalty = 0.0
        if frame_id in used_frames or occurrence.asset_id in used_assets:
            penalty = 0.35
        if occurrence.cluster_id and occurrence.cluster_id in used_assets:
            penalty = 0.35
        if frame_id in clean_ids:
            penalty -= 0.45
        elif clean_ids:
            penalty += 0.75
        if role == "connective" and _frame_is_pedagogical_connective(frame_id, visual, transcript):
            penalty -= 0.8
        scored.append((_frame_score(unit, occurrence, by_candidate.get(frame_id)) - penalty, frame_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    wanted = 2 if unit.kind == "comparison" else 3 if unit.kind == "procedure" else 1

    def allowed(frame_id: str) -> bool:
        if role == "connective" and (
            _frame_is_conversation(frame_id, visual, transcript)
            or _frame_shows_rate(frame_id, visual, transcript)
        ):
            return False
        if role == "cause" and _frame_shows_rate(frame_id, visual, transcript):
            return False
        return True

    picked: list[str] = []
    for _score, frame_id in scored:
        if not allowed(frame_id):
            continue
        occurrence = occurrences[frame_id]
        if frame_id in used_frames or occurrence.asset_id in used_assets:
            continue
        picked.append(frame_id)
        if len(picked) >= wanted:
            break
    if unit.kind == "comparison":
        if len(picked) >= 2:
            return picked[:2], "comparison"
        return [], "text"
    if unit.kind == "procedure":
        if len(picked) >= 2:
            return picked[:3], "sequence"
        return (picked[:1], "image-text") if picked else ([], "text")
    if picked:
        return picked[:1], "image-text"
    return [], "text"


def score_candidates(
    knowledge: KnowledgeDocument,
    *,
    visual: VisualCatalogue,
    course_map: CourseMap | None = None,
) -> list[PageCandidate]:
    del course_map
    parents = prerequisite_parents(knowledge)
    required_children = {parent for child, items in parents.items() for parent in items}
    candidates: list[PageCandidate] = []
    for unit in knowledge.units:
        score, reason = _score_unit(unit)
        raw_title = unit.claims[0].text if unit.claims else unit.kind
        title = sanitize_student_copy(raw_title)[:80] or raw_title[:80]
        notes = " ".join(claim.text for claim in unit.claims)[:4000]
        body = [
            sanitize_student_copy(claim.text)[:200]
            for claim in unit.claims[:4]
            if sanitize_student_copy(claim.text).strip()
        ]
        candidates.append(
            PageCandidate(
                id=f"intent-{unit.id}",
                unit_id=unit.id,
                topic_id=unit.topic_id,
                kind=unit.kind,
                claim_ids=[claim.id for claim in unit.claims],
                frame_ids=[],
                layout="text",
                start_seconds=unit.start_seconds,
                end_seconds=unit.end_seconds,
                score=score,
                required_prerequisite=unit.id in required_children,
                selection_reason=reason,
                title=title,
                notes=notes,
                body_points=body,
            )
        )
    return candidates


def _example_keep(
    candidates: Iterable[PageCandidate],
    *,
    expanded_cause_units: set[str] | None = None,
) -> set[str]:
    per_topic: dict[str, list[PageCandidate]] = {}
    for item in candidates:
        if item.kind != "example":
            continue
        per_topic.setdefault(item.topic_id, []).append(item)
    kept: set[str] = set()
    for rows in per_topic.values():
        ranked = sorted(rows, key=lambda row: (-row.score, row.start_seconds, row.id))
        for row in ranked[:_MAX_EXAMPLES_PER_TOPIC]:
            kept.add(row.unit_id)
    expanded = sorted(
        (row for rows in per_topic.values() for row in rows
         if row.unit_id in (expanded_cause_units or set())),
        key=lambda row: (-row.score, row.start_seconds, row.id),
    )
    kept.update(
        row.unit_id for row in expanded[: 2 * _MAX_CAUSE_CONTENT_PAGES]
    )
    return kept


def _cause_example_signature(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
) -> str | None:
    if item.kind != "example":
        return None
    unit = _unit_by_id(knowledge, item.unit_id)
    if unit is None:
        return None
    sentence = next(
        (
            sanitize_student_copy(claim.text).strip()
            for claim in unit.claims
            if claim.id in item.claim_ids
            and claim.provenance == "source"
            and _script_profile(claim.text) == "ja"
            and "につき" in claim.text
        ),
        None,
    )
    if not sentence:
        return None
    return re.sub(r"\s+", "", sentence).rstrip("。．.!！?？")


_KANA_RE = re.compile(r"[\u3040-\u30ff\u3100-\u312f]")


def _script_profile(text: str) -> str:
    if _KANA_RE.search(text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "other"


def _time_overlap_ratio(
    a_start: float,
    a_end: float,
    b_start: float,
    b_end: float,
) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0.0
    span = max(1e-6, min(a_end - a_start, b_end - b_start))
    return (end - start) / span


def _evidence_jaccard(ids_a: list[str], ids_b: list[str]) -> float:
    a, b = set(ids_a), set(ids_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _claim_evidence_ids(unit: KnowledgeUnit) -> list[str]:
    return [item for claim in unit.claims for item in claim.evidence_ids]


def _translation_duplicate_pair(unit_a: KnowledgeUnit, unit_b: KnowledgeUnit) -> bool:
    if unit_a.id == unit_b.id or unit_a.topic_id != unit_b.topic_id:
        return False
    if _time_overlap_ratio(
        unit_a.start_seconds,
        unit_a.end_seconds,
        unit_b.start_seconds,
        unit_b.end_seconds,
    ) < 0.45:
        return False
    if _evidence_jaccard(_claim_evidence_ids(unit_a), _claim_evidence_ids(unit_b)) < 0.45:
        return False
    text_a = " ".join(claim.text for claim in unit_a.claims)
    text_b = " ".join(claim.text for claim in unit_b.claims)
    profile_a, profile_b = _script_profile(text_a), _script_profile(text_b)
    if {profile_a, profile_b} == {"ja", "zh"}:
        return True
    if profile_a == profile_b and profile_a != "other":
        normalized_a = re.sub(r"\s+", "", text_a)
        normalized_b = re.sub(r"\s+", "", text_b)
        if normalized_a and normalized_b:
            shorter, longer = (
                (normalized_a, normalized_b)
                if len(normalized_a) <= len(normalized_b)
                else (normalized_b, normalized_a)
            )
            if shorter in longer or longer in shorter:
                return True
    return False


def _demote_translation_duplicate_units(
    candidates: list[PageCandidate],
    knowledge: KnowledgeDocument,
) -> set[str]:
    demoted: set[str] = set()
    units = {
        item.unit_id: _unit_by_id(knowledge, item.unit_id)
        for item in candidates
    }
    for index, left in enumerate(candidates):
        unit_left = units.get(left.unit_id)
        if unit_left is None or left.unit_id in demoted:
            continue
        for right in candidates[index + 1 :]:
            unit_right = units.get(right.unit_id)
            if unit_right is None or right.unit_id in demoted:
                continue
            if not _translation_duplicate_pair(unit_left, unit_right):
                continue
            if left.score > right.score:
                loser = right
            elif right.score > left.score:
                loser = left
            elif unit_left.kind == "example" and unit_right.kind != "example":
                loser = left
            elif unit_right.kind == "example" and unit_left.kind != "example":
                loser = right
            else:
                loser = right if left.start_seconds <= right.start_seconds else left
            demoted.add(loser.unit_id)
    return demoted


def _topic_by_id(
    topic_id: str,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> Topic | None:
    if course_map and course_map.topics:
        found = next((item for item in course_map.topics if item.id == topic_id), None)
        if found is not None:
            return found
    units = [unit for unit in knowledge.units if unit.topic_id == topic_id]
    if not units:
        return None
    return Topic(
        id=topic_id,
        title=topic_id,
        goal=topic_id,
        start_seconds=min(unit.start_seconds for unit in units),
        end_seconds=max(unit.end_seconds for unit in units),
        evidence_ids=[],
    )


def _claim_topic_ids(knowledge: KnowledgeDocument) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for unit in knowledge.units:
        for claim in unit.claims:
            mapping[claim.id] = unit.topic_id
    return mapping


def _restore_sense_content_titles(
    plan: EditorialPlan,
    *,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> EditorialPlan:
    claim_topics = _claim_topic_ids(knowledge)
    pages: list[PageIntent] = []
    for page in plan.pages:
        if page.type != "content":
            pages.append(page)
            continue
        topic_id = next(
            (claim_topics[claim_id] for claim_id in page.claim_ids if claim_id in claim_topics),
            None,
        )
        if topic_id is None:
            pages.append(page)
            continue
        unit = _unit_for_claim_ids(knowledge, page.claim_ids)
        topic = _topic_by_id(topic_id, course_map, knowledge) if unit is not None else None
        if is_tsuki_grammar_lesson(course_map, knowledge) and unit is not None:
            if text_is_rate_only(_unit_display_text(unit)):
                pages.append(page.model_copy(update={"title": sense_heading(2)}))
                continue
            if unit_is_connective_attachment(unit, topic):
                pages.append(page.model_copy(update={"title": CONNECTIVE_HEADING}))
                continue
        connective = topic_for_connective(course_map, knowledge)
        if connective is not None and topic_id == connective.id:
            pages.append(page.model_copy(update={"title": CONNECTIVE_HEADING}))
            continue
        ordinal = sense_ordinal(topic_id, course_map, knowledge=knowledge)
        if ordinal is None:
            pages.append(page)
            continue
        pages.append(page.model_copy(update={"title": sense_heading(ordinal)}))
    return plan.model_copy(update={"pages": pages})


def _merge_summary_body_points(base: PageIntent, incoming: PageIntent) -> list[str]:
    merged: list[str] = []
    for index, base_point in enumerate(base.body_points):
        incoming_point = (
            incoming.body_points[index] if index < len(incoming.body_points) else ""
        )
        cleaned = sanitize_student_copy(incoming_point).strip()
        if cleaned and is_fixed_sense_summary_line(base_point) and not is_fixed_sense_summary_line(
            cleaned
        ):
            merged.append(sanitize_student_copy(base_point)[:200])
        elif cleaned:
            merged.append(cleaned[:200])
        else:
            merged.append(base_point)
    return merged or list(base.body_points)


def _reserved_topic_ids(
    candidates: list[PageCandidate],
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[str]:
    mandatory = sense_topic_ids(course_map, knowledge)
    seen = set(mandatory)
    if course_map and course_map.topics:
        rest = [topic.id for topic in course_map.topics if topic.id not in seen]
        return mandatory + rest
    return mandatory + sorted(
        {item.topic_id for item in candidates if item.topic_id not in seen}
    )


def _is_meta_page_candidate(item: PageCandidate, knowledge: KnowledgeDocument) -> bool:
    unit = _unit_by_id(knowledge, item.unit_id)
    if unit is None:
        return contains_student_meta(item.title) or contains_student_meta(item.notes)
    return _meta_filler_penalty(unit) >= 2.0


def _candidate_from_unit(
    unit: KnowledgeUnit,
    knowledge: KnowledgeDocument,
    *,
    ranked: list[PageCandidate],
    lock_note: str = "mandatory-sense lock",
) -> PageCandidate:
    existing = next((row for row in ranked if row.unit_id == unit.id), None)
    if existing is not None:
        return replace(
            existing,
            selection_reason=f"{lock_note}; {existing.selection_reason}"[:400],
        )
    parents = prerequisite_parents(knowledge)
    required_children = {parent for child, items in parents.items() for parent in items}
    score, reason = _score_unit(unit)
    raw_title = unit.claims[0].text if unit.claims else unit.kind
    title = sanitize_student_copy(raw_title)[:80] or raw_title[:80]
    body = [
        sanitize_student_copy(claim.text)[:200]
        for claim in unit.claims[:4]
        if sanitize_student_copy(claim.text).strip() or claim.text.strip()
    ]
    if not body and unit.claims:
        body = [unit.claims[0].text[:200]]
    return PageCandidate(
        id=f"intent-{unit.id}",
        unit_id=unit.id,
        topic_id=unit.topic_id,
        kind=unit.kind,
        claim_ids=[claim.id for claim in unit.claims],
        frame_ids=[],
        layout="text",
        start_seconds=unit.start_seconds,
        end_seconds=unit.end_seconds,
        score=score,
        required_prerequisite=unit.id in required_children,
        selection_reason=f"{lock_note}; {reason}"[:400],
        title=title,
        notes=" ".join(claim.text for claim in unit.claims)[:4000],
        body_points=body,
    )


def _mandatory_unit_for_topic(
    topic: Topic,
    knowledge: KnowledgeDocument,
    *,
    course_map: CourseMap | None,
) -> KnowledgeUnit | None:
    unit = pick_summary_unit_for_topic(topic, knowledge, course_map=course_map)
    if unit is not None:
        return unit
    units = sorted(
        [item for item in knowledge.units if item.topic_id == topic.id],
        key=lambda item: (
            0 if item.kind == "example" else 1,
            item.start_seconds,
            item.id,
        ),
    )
    for candidate in units:
        if not candidate.claims:
            continue
        if sanitize_student_copy(candidate.claims[0].text).strip() or candidate.claims[0].text.strip():
            return candidate
    return units[0] if units else None


def _pick_mandatory_sense_candidate(
    topic_id: str,
    ranked: list[PageCandidate],
    *,
    demoted: set[str],
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
    lock_note: str = "mandatory-sense lock",
    ordinal: int | None = None,
) -> PageCandidate | None:
    want_connective = "mandatory-connective" in lock_note

    def matches_ordinal(row: PageCandidate) -> bool:
        if ordinal == 1 and (
            _row_is_rate_only(row, knowledge) or _row_has_rate_snippet(row, knowledge)
        ):
            return False
        if ordinal == 2 and not _row_is_rate_only(row, knowledge):
            return False
        return True

    if ordinal == 2:
        rate_rows = [
            row
            for row in ranked
            if _row_is_rate_only(row, knowledge) and row.unit_id not in demoted
        ]
        on_topic = [row for row in rate_rows if row.topic_id == topic_id]
        rate_pool = on_topic or rate_rows
        examples = [row for row in rate_pool if row.kind == "example"]
        chosen_pool = examples or rate_pool
        if chosen_pool:
            row = sorted(chosen_pool, key=lambda item: (-item.score, item.start_seconds, item.id))[0]
            return replace(
                row,
                selection_reason=f"{lock_note}; {row.selection_reason}"[:400],
            )

    def narrow(pool: list[PageCandidate]) -> list[PageCandidate]:
        if want_connective:
            matched = [
                row
                for row in pool
                if _candidate_is_connective(row, knowledge, course_map)
            ]
            return matched or pool
        plain = [
            row
            for row in pool
            if not _candidate_is_connective(row, knowledge, course_map) and matches_ordinal(row)
        ]
        if ordinal in {1, 2}:
            return plain
        return plain or pool

    pools = [
        narrow(
            [
                item
                for item in ranked
                if item.topic_id == topic_id
                and item.unit_id not in demoted
                and not _is_meta_page_candidate(item, knowledge)
            ]
        ),
        narrow(
            [
                item
                for item in ranked
                if item.topic_id == topic_id and item.unit_id not in demoted
            ]
        ),
        narrow([item for item in ranked if item.topic_id == topic_id]),
    ]
    for pool in pools:
        examples = sorted(
            [row for row in pool if row.kind == "example"],
            key=lambda row: (-row.score, row.start_seconds, row.id),
        )
        if examples:
            row = examples[0]
            return replace(
                row,
                selection_reason=f"{lock_note}; {row.selection_reason}"[:400],
            )
        if pool:
            pick = sorted(pool, key=lambda row: (-row.score, row.start_seconds, row.id))[0]
            return replace(
                pick,
                selection_reason=f"{lock_note}; {pick.selection_reason}"[:400],
            )
    topic = _topic_by_id(topic_id, course_map, knowledge)
    if topic is None:
        return None
    unit = _mandatory_unit_for_topic(topic, knowledge, course_map=course_map)
    if unit is None:
        return None
    unit_text = _unit_display_text(unit)
    if ordinal == 2 and not text_is_rate_only(unit_text):
        return None
    if ordinal == 1 and text_is_rate_only(unit_text):
        return None
    return _candidate_from_unit(unit, knowledge, ranked=ranked, lock_note=lock_note)


def _selected_sense_ordinals(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> set[int]:
    ordinals: set[int] = set()
    for item in selected:
        ordinal = _candidate_ordinal(item, course_map, knowledge)
        if ordinal is not None:
            ordinals.add(ordinal)
    return ordinals


def _drop_weakest_page_for_sense(
    selected: list[PageCandidate],
    ordinal: int,
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
    protected_unit_ids: set[str],
) -> list[PageCandidate]:
    matches = [
        item
        for item in selected
        if sense_ordinal(item.topic_id, course_map, knowledge=knowledge) == ordinal
        and item.unit_id not in protected_unit_ids
        and "mandatory-sense lock" not in item.selection_reason
        and not _candidate_is_connective(item, knowledge, course_map)
        and not _row_is_rate_only(item, knowledge)
    ]
    if len(matches) < 2:
        return selected
    weakest = min(matches, key=lambda row: (row.score, row.start_seconds, row.id))
    return [item for item in selected if item.unit_id != weakest.unit_id]


def _enforce_mandatory_sense_pages(
    selected: list[PageCandidate],
    *,
    ranked: list[PageCandidate],
    demoted: set[str],
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
    content_max: int,
) -> list[PageCandidate]:
    selected_ids = {item.unit_id for item in selected}
    updated = list(selected)
    for ordinal in (1, 2, 3):
        if ordinal in _selected_sense_ordinals(
            updated, course_map=course_map, knowledge=knowledge
        ):
            continue
        topic = topic_for_sense_ordinal(course_map, ordinal, knowledge=knowledge)
        if topic is None:
            continue
        if not any(unit.topic_id == topic.id for unit in knowledge.units):
            continue
        pick = _pick_mandatory_sense_candidate(
            topic.id,
            ranked,
            demoted=demoted,
            knowledge=knowledge,
            course_map=course_map,
            ordinal=ordinal,
        )
        if pick is None:
            continue
        if _candidate_page_count(updated, course_map=course_map, knowledge=knowledge) >= content_max:
            if ordinal == 2:
                updated = _drop_weakest_page_for_sense(
                    updated,
                    1,
                    course_map=course_map,
                    knowledge=knowledge,
                    protected_unit_ids=selected_ids,
                )
            elif _candidate_page_count(updated, course_map=course_map, knowledge=knowledge) >= content_max:
                updated = sorted(
                    updated,
                    key=lambda row: (row.score, row.start_seconds, row.id),
                )[1:]
        if pick.unit_id in {item.unit_id for item in updated}:
            continue
        updated.append(pick)
        selected_ids.add(pick.unit_id)
    return updated


def _pick_topic_representative(
    pool: list[PageCandidate],
    *,
    keep_examples: set[str],
    knowledge: KnowledgeDocument,
) -> PageCandidate | None:
    usable = [row for row in pool if not _is_meta_page_candidate(row, knowledge)]
    if not usable:
        return None
    examples = [
        row
        for row in usable
        if row.kind == "example" and row.unit_id in keep_examples
    ]
    if examples:
        return sorted(examples, key=lambda row: (-row.score, row.start_seconds, row.id))[0]
    non_recap = [row for row in usable if row.kind != "recap"]
    if non_recap:
        return non_recap[0]
    return usable[0]


def _cause_keep_key(item: PageCandidate, knowledge: KnowledgeDocument) -> tuple:
    """Lower sorts first: locked example pages survive the 用法一 cap."""

    locked = 0 if "mandatory-sense lock" in item.selection_reason else 1
    example = 0 if item.kind == "example" else 1
    meta = 1 if _is_meta_page_candidate(item, knowledge) else 0
    return (locked, example, meta, -item.score, item.start_seconds, item.id)


def _stamp_connective_lock(
    selected: list[PageCandidate],
    unit_ids: set[str],
) -> list[PageCandidate]:
    stamped: list[PageCandidate] = []
    for item in selected:
        if item.unit_id not in unit_ids or "mandatory-connective lock" in item.selection_reason:
            stamped.append(item)
            continue
        stamped.append(
            replace(
                item,
                selection_reason=f"mandatory-connective lock; {item.selection_reason}"[:400],
            )
        )
    return stamped


def _drop_duplicate_cause_page(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[PageCandidate]:
    """Drop the weakest extra 用法一 page, keeping the locked example."""

    cause = [
        item
        for item in selected
        if _is_usage_cause_page(item, course_map, knowledge)
    ]
    if len(cause) < 2:
        return selected
    worst = max(cause, key=lambda row: _cause_keep_key(row, knowledge))
    return [item for item in selected if item.unit_id != worst.unit_id]


def _drop_nonprotected_for_connective(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[PageCandidate]:
    """Drop a filler page so 接续 can fit without removing 用法二/三 or the last 用法一."""

    cause = [
        item
        for item in selected
        if _is_usage_cause_page(item, course_map, knowledge)
    ]
    protected_cause = {
        min(cause, key=lambda row: _cause_keep_key(row, knowledge)).unit_id
    } if cause else set()

    def droppable(item: PageCandidate) -> bool:
        if _candidate_is_connective(item, knowledge, course_map):
            return False
        ordinal = sense_ordinal(item.topic_id, course_map, knowledge=knowledge)
        if ordinal in {2, 3}:
            return False
        if ordinal == 1 and item.unit_id in protected_cause:
            return False
        return True

    candidates = [item for item in selected if droppable(item)]
    if not candidates:
        return selected
    weakest = min(candidates, key=lambda row: (row.score, -row.start_seconds, row.id))
    return [item for item in selected if item.unit_id != weakest.unit_id]


def _connective_units(
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> list[KnowledgeUnit]:
    if not is_tsuki_grammar_lesson(course_map, knowledge):
        return []
    topics = {topic.id: topic for topic in (course_map.topics if course_map else [])}
    found: list[KnowledgeUnit] = []
    for unit in knowledge.units:
        topic = topics.get(unit.topic_id) or _topic_by_id(unit.topic_id, course_map, knowledge)
        if unit_is_connective_attachment(unit, topic):
            found.append(unit)
    return found


def _pick_connective_candidate(
    ranked: list[PageCandidate],
    *,
    demoted: set[str],
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> PageCandidate | None:
    """Best attachment unit, including glosses filed under a cause topic."""

    units = _connective_units(knowledge, course_map)
    if units:
        def rank(unit: KnowledgeUnit) -> tuple:
            text = " ".join(claim.text for claim in unit.claims)
            meta = 1 if contains_student_meta(text) else 0
            example = 0 if unit.kind == "example" else 1
            return (meta, example, unit.start_seconds, unit.id)

        best = min(units, key=rank)
        existing = next((row for row in ranked if row.unit_id == best.id), None)
        if existing is not None:
            return replace(
                existing,
                selection_reason=f"mandatory-connective lock; {existing.selection_reason}"[:400],
            )
        return _candidate_from_unit(
            best,
            knowledge,
            ranked=ranked,
            lock_note="mandatory-connective lock",
        )
    topic = topic_for_connective(course_map, knowledge)
    if topic is None:
        return None
    return _pick_mandatory_sense_candidate(
        topic.id,
        ranked,
        demoted=demoted,
        knowledge=knowledge,
        course_map=course_map,
        lock_note="mandatory-connective lock",
    )


def _ensure_connective_page(
    selected: list[PageCandidate],
    *,
    ranked: list[PageCandidate],
    demoted: set[str],
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
    content_max: int,
) -> list[PageCandidate]:
    """Force one 接续 page when attachment knowledge exists, on any topic."""

    pick = _pick_connective_candidate(
        ranked,
        demoted=demoted,
        knowledge=knowledge,
        course_map=course_map,
    )
    if pick is None:
        return selected
    present = [
        item
        for item in selected
        if item.unit_id == pick.unit_id or _candidate_is_connective(item, knowledge, course_map)
    ]
    if present:
        return _stamp_connective_lock(selected, {item.unit_id for item in present})
    updated = list(selected)
    if _candidate_page_count(updated, course_map=course_map, knowledge=knowledge) >= content_max:
        updated = _drop_duplicate_cause_page(
            updated,
            course_map=course_map,
            knowledge=knowledge,
        )
    if _candidate_page_count(updated, course_map=course_map, knowledge=knowledge) >= content_max:
        updated = _drop_nonprotected_for_connective(
            updated,
            course_map=course_map,
            knowledge=knowledge,
        )
    if _candidate_page_count(updated, course_map=course_map, knowledge=knowledge) >= content_max:
        return updated
    if any(item.unit_id == pick.unit_id for item in updated):
        return _stamp_connective_lock(updated, {pick.unit_id})
    updated.append(pick)
    return updated


def _rank_key(item: PageCandidate, *, demoted: set[str]) -> tuple[float, float, str]:
    penalty = 5.0 if item.unit_id in demoted else 0.0
    return (-(item.score - penalty), item.start_seconds, item.id)


def select_candidates(
    candidates: list[PageCandidate],
    *,
    target_pages: int,
    max_pages: int,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None = None,
    valid_evidence_ids: set[str] | None = None,
) -> tuple[list[PageCandidate], list[Omission]]:
    sense_slots = len(sense_topic_ids(course_map, knowledge))
    content_target = max(1, min(target_pages, max_pages) - 2)
    content_max = max(1, max_pages - 2, sense_slots)
    parents = prerequisite_parents(knowledge)
    demoted = _demote_translation_duplicate_units(candidates, knowledge)
    unsupported_cause_ids: set[str] = set()
    invalid_cause_claims: dict[str, tuple[str, ...]] = {}
    expanded_cause_units: set[str] = set()
    ranked_candidates: list[PageCandidate] = []
    for item in candidates:
        if (
            valid_evidence_ids is None
            or not _is_usage_cause_page(item, course_map, knowledge)
        ):
            ranked_candidates.append(item)
            continue
        unit = _unit_by_id(knowledge, item.unit_id)
        source_claims = [
            claim
            for claim in (unit.claims if unit else [])
            if claim.id in item.claim_ids
            and claim.provenance == "source"
            and set(claim.evidence_ids) & valid_evidence_ids
        ]
        other_claim_ids = tuple(
            claim_id for claim_id in item.claim_ids
            if claim_id not in {claim.id for claim in source_claims}
        )
        if other_claim_ids:
            invalid_cause_claims[item.unit_id] = other_claim_ids
        if not source_claims:
            unsupported_cause_ids.add(item.unit_id)
            continue
        bullets = _learner_bullets_for_claims(source_claims)
        if not bullets or any(len(bullet) > 200 for bullet in bullets):
            invalid_cause_claims[item.unit_id] = tuple(item.claim_ids)
            unsupported_cause_ids.add(item.unit_id)
            continue
        if (
            valid_evidence_ids is not None
            and item.kind == "example"
            and _is_usage_cause_page(item, course_map, knowledge)
            and any(
                _script_profile(claim.text) == "ja" and "につき" in claim.text
                for claim in source_claims
            )
        ):
            expanded_cause_units.add(item.unit_id)
        ranked_candidates.append(
            replace(
                item,
                claim_ids=[claim.id for claim in source_claims],
                body_points=bullets,
                notes="。".join(bullets),
            )
        )
    ranked = sorted(
        [item for item in ranked_candidates if item.unit_id not in unsupported_cause_ids],
        key=lambda item: _rank_key(item, demoted=demoted),
    )
    deduplicated: list[PageCandidate] = []
    seen_cause_examples: set[str] = set()
    duplicate_cause_ids: set[str] = set()
    for item in ranked:
        signature = (
            _cause_example_signature(item, knowledge)
            if item.unit_id in expanded_cause_units
            else None
        )
        if signature is not None and signature in seen_cause_examples:
            duplicate_cause_ids.add(item.unit_id)
            continue
        if signature is not None:
            seen_cause_examples.add(signature)
        deduplicated.append(item)
    ranked = deduplicated
    keep_examples = _example_keep(
        ranked,
        expanded_cause_units=expanded_cause_units - duplicate_cause_ids,
    )
    selected: list[PageCandidate] = []
    selected_ids: set[str] = set()

    def cause_example_count() -> int:
        return sum(
            1
            for row in selected
            if row.kind == "example"
            and _is_usage_cause_page(row, course_map, knowledge)
        )

    def about_count() -> int:
        return sum(
            1 for row in selected if _is_usage_about_page(row, course_map, knowledge)
        )

    def add(item: PageCandidate, *, force: bool = False) -> None:
        if item.unit_id in selected_ids:
            return
        if item.unit_id in demoted and not force:
            return
        if (
            item.kind == "example"
            and _is_usage_cause_page(item, course_map, knowledge)
            and cause_example_count() >= 2 * _MAX_CAUSE_CONTENT_PAGES
        ):
            return
        if (
            not force
            and _is_usage_about_page(item, course_map, knowledge)
            and about_count() >= _MAX_ABOUT_CONTENT_PAGES
        ):
            return
        if (
            not force
            and is_tsuki_grammar_lesson(course_map, knowledge)
            and (_row_is_rate_only(item, knowledge) or _row_has_rate_snippet(item, knowledge))
            and 2 in _selected_sense_ordinals(selected, course_map=course_map, knowledge=knowledge)
        ):
            return
        if (
            _candidate_page_count(
                [*selected, item],
                course_map=course_map,
                knowledge=knowledge,
            )
            > content_max
            and not force
        ):
            return
        selected.append(item)
        selected_ids.add(item.unit_id)

    def add_with_prerequisites(item: PageCandidate, *, force: bool = False) -> None:
        for parent_id in sorted(parents.get(item.unit_id, set())):
            parent = next((row for row in ranked if row.unit_id == parent_id), None)
            if parent is not None:
                add(parent, force=force)
        add(item, force=force)

    for ordinal in (1, 2, 3):
        if ordinal in _selected_sense_ordinals(selected, course_map=course_map, knowledge=knowledge):
            continue
        topic = topic_for_sense_ordinal(course_map, ordinal, knowledge=knowledge)
        if topic is None or not any(unit.topic_id == topic.id for unit in knowledge.units):
            continue
        if ordinal != 2 and any(row.topic_id == topic.id for row in selected):
            continue
        pick = _pick_mandatory_sense_candidate(
            topic.id,
            ranked,
            demoted=demoted,
            knowledge=knowledge,
            course_map=course_map,
            ordinal=ordinal,
        )
        if pick is not None:
            add_with_prerequisites(pick, force=True)

    selected = _enforce_mandatory_sense_pages(
        selected,
        ranked=ranked,
        demoted=demoted,
        knowledge=knowledge,
        course_map=course_map,
        content_max=content_max,
    )
    selected_ids = {item.unit_id for item in selected}

    for topic_id in _reserved_topic_ids(candidates, course_map, knowledge):
        pool = [
            item
            for item in ranked
            if item.topic_id == topic_id
            and item.unit_id not in selected_ids
            and item.unit_id not in demoted
        ]
        if not pool:
            continue
        pick = _pick_topic_representative(pool, keep_examples=keep_examples, knowledge=knowledge)
        if pick is None:
            continue
        if pick.kind == "example" and pick.unit_id not in keep_examples:
            non_example = next(
                (
                    row
                    for row in pool
                    if row.kind != "example" and not _is_meta_page_candidate(row, knowledge)
                ),
                None,
            )
            if non_example is not None:
                pick = non_example
        add_with_prerequisites(pick)
        topic_examples = sorted(
            [
                row
                for row in pool
                if row.kind == "example"
                and row.unit_id in keep_examples
                and row.unit_id not in selected_ids
            ],
            key=lambda row: (row.start_seconds, row.id),
        )
        for extra in topic_examples:
            if extra.unit_id == pick.unit_id:
                continue
            add_with_prerequisites(extra)
            if _candidate_page_count(selected, course_map=course_map, knowledge=knowledge) >= content_max:
                break

    for item in ranked:
        if _is_meta_page_candidate(item, knowledge):
            continue
        if item.kind == "example" and item.unit_id not in keep_examples:
            continue
        if item.unit_id in selected_ids:
            continue
        if (
            _candidate_page_count(selected, course_map=course_map, knowledge=knowledge)
            >= content_target
            and item.unit_id not in selected_ids
        ):
            if item.required_prerequisite:
                add_with_prerequisites(item)
            continue
        add_with_prerequisites(item)

    selected = _ensure_connective_page(
        selected,
        ranked=ranked,
        demoted=demoted,
        knowledge=knowledge,
        course_map=course_map,
        content_max=content_max,
    )
    selected_ids = {item.unit_id for item in selected}
    capacity_omission_ids = {
        item.unit_id
        for item in candidates
        if item.kind == "example"
        and _is_usage_cause_page(item, course_map, knowledge)
        and item.unit_id not in selected_ids
        and item.unit_id not in unsupported_cause_ids
        and cause_example_count() >= 2 * _MAX_CAUSE_CONTENT_PAGES
    }
    omissions: list[Omission] = []
    for item in candidates:
        for claim_id in invalid_cause_claims.get(item.unit_id, ()):
            omissions.append(
                Omission(
                    topic_id=item.topic_id,
                    claim_id=claim_id,
                    reason=_cause_omission_reason(item, knowledge, valid_evidence_ids),
                )
            )
    for item in candidates:
        if item.unit_id in selected_ids:
            continue
        for claim_id in item.claim_ids:
            if any(omission.claim_id == claim_id for omission in omissions):
                continue
            omissions.append(
                Omission(
                    topic_id=item.topic_id,
                    claim_id=claim_id,
                    reason=(
                        _cause_omission_reason(item, knowledge, valid_evidence_ids)
                        if item.unit_id in unsupported_cause_ids
                        else "duplicate source-backed cause example"
                        if item.unit_id in duplicate_cause_ids
                        else "cause-example capacity (four supported examples across two pages)"
                        if item.unit_id in capacity_omission_ids
                        else "page budget or representative-example compression"
                    ),
                )
            )
    return selected, omissions


def _learner_bullets_for_claims(claims: list[object]) -> list[str]:
    """Pair source-language text and its existing translation in one bullet."""

    lines = [sanitize_student_copy(str(getattr(claim, "text", ""))).strip() for claim in claims]
    lines = [line for line in lines if line]
    japanese = next((line for line in lines if _script_profile(line) == "ja"), None)
    chinese = next((line for line in lines if _script_profile(line) == "zh"), None)
    if japanese and chinese:
        paired = f"{japanese}（{chinese}）"
        return [paired, *(line for line in lines if line not in {japanese, chinese})]
    return lines


def _group_cause_candidates(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[PageCandidate]:
    """Represent up to two source-backed examples on each of at most two pages."""

    cause = [item for item in selected if _is_usage_cause_page(item, course_map, knowledge)]
    if len(cause) < 2:
        return selected
    examples = [item for item in cause if item.kind == "example"][: 2 * _MAX_CAUSE_CONTENT_PAGES]
    supporting = [item for item in cause if item.kind != "example"]
    groups = [examples[index : index + 2] for index in range(0, len(examples), 2)]
    if not groups:
        groups = [supporting[:2]]
        supporting = supporting[2:]
    grouped: dict[str, PageCandidate] = {}
    consumed = {item.unit_id for item in cause}

    def group_claims(group: list[PageCandidate]) -> list[object]:
        return [
            claim
            for member in group
            for claim in (
                _unit_by_id(knowledge, member.unit_id).claims
                if _unit_by_id(knowledge, member.unit_id)
                else []
            )
            if claim.id in member.claim_ids
        ]

    def group_bullet_count(group: list[PageCandidate]) -> int:
        return len(_learner_bullets_for_claims(group_claims(group)))

    for item in supporting:
        unit = _unit_by_id(knowledge, item.unit_id)
        if unit is None:
            continue
        points = _learner_bullets_for_claims(
            [claim for claim in unit.claims if claim.id in item.claim_ids]
        )
        target = next(
            (
                group for group in groups
                if group_bullet_count(group) + len(points) <= 4
                and len([*group_claims(group), *unit.claims]) <= 8
            ),
            None,
        )
        if target is not None and all(len(point) <= 200 for point in points):
            target.append(item)
    for group in groups[:_MAX_CAUSE_CONTENT_PAGES]:
        first = group[0]
        claim_ids: list[str] = []
        frame_ids: list[str] = []
        body_points: list[str] = []
        for item in group:
            claim_ids.extend(item.claim_ids)
            frame_ids.extend(item.frame_ids)
            unit = _unit_by_id(knowledge, item.unit_id)
            if unit:
                claims = [claim for claim in unit.claims if claim.id in item.claim_ids]
                bullets = _learner_bullets_for_claims(claims)
                if all(len(point) <= 200 for point in bullets):
                    body_points.extend(bullets)
                else:
                    claim_ids = claim_ids[:-len(item.claim_ids)]
        grouped[first.unit_id] = replace(
            first,
            id=f"intent-cause-group-{first.unit_id}",
            claim_ids=list(dict.fromkeys(claim_ids))[:8],
            frame_ids=list(dict.fromkeys(frame_ids))[:3],
            body_points=body_points[:4],
            notes="。".join(body_points)[:4000],
            selection_reason="source-backed cause examples; " + first.selection_reason[:300],
        )
    result: list[PageCandidate] = []
    inserted_groups: set[str] = set()
    for item in selected:
        if item.unit_id not in consumed:
            result.append(item)
            continue
        owner = next((group[0].unit_id for group in groups[:_MAX_CAUSE_CONTENT_PAGES] if item in group), None)
        if owner and owner not in inserted_groups:
            result.append(grouped[owner])
            inserted_groups.add(owner)
    return result


def _candidate_page_count(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> int:
    """Count the pages candidates will occupy after cause examples are grouped."""

    return len(
        _group_cause_candidates(
            selected,
            course_map=course_map,
            knowledge=knowledge,
        )
    )


def _cause_omission_reason(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
    valid_evidence_ids: set[str] | None,
) -> str:
    unit = _unit_by_id(knowledge, item.unit_id)
    claims = [claim for claim in (unit.claims if unit else []) if claim.id in item.claim_ids]
    if valid_evidence_ids is None or any(
        claim.provenance != "source"
        or not set(claim.evidence_ids) & valid_evidence_ids
        for claim in claims
    ):
        return "source evidence is unavailable for this example"
    bullets = _learner_bullets_for_claims(claims)
    if bullets and any(len(bullet) > 200 for bullet in bullets):
        return "learner copy exceeds the 200-character limit; review required"
    return "source claim lacks valid evidence or source provenance"


def sort_candidates(
    selected: list[PageCandidate],
    *,
    order: DeckOrder,
    knowledge: KnowledgeDocument,
) -> list[PageCandidate]:
    if order == "chronological":
        return sorted(selected, key=lambda item: (item.start_seconds, item.id))
    parents = prerequisite_parents(knowledge)
    remaining = {item.unit_id: item for item in selected}
    incoming = {item.unit_id: 0 for item in selected}
    for item in selected:
        for parent in parents.get(item.unit_id, set()):
            if parent in remaining:
                incoming[item.unit_id] += 1
    ordered: list[PageCandidate] = []
    available = [
        item
        for item in selected
        if incoming[item.unit_id] == 0
    ]
    available.sort(key=lambda item: (item.start_seconds, item.id))
    while available:
        current = available.pop(0)
        ordered.append(current)
        remaining.pop(current.unit_id, None)
        for item in list(remaining.values()):
            if current.unit_id in parents.get(item.unit_id, set()):
                incoming[item.unit_id] -= 1
                if incoming[item.unit_id] == 0:
                    available.append(item)
                    available.sort(key=lambda row: (row.start_seconds, row.id))
    if remaining:
        ordered.extend(sorted(remaining.values(), key=lambda item: (item.start_seconds, item.id)))
    return ordered


def _candidate_role(
    item: PageCandidate,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> str:
    if _candidate_is_connective(item, knowledge, course_map):
        return "connective"
    if _row_is_rate_only(item, knowledge):
        return "rate"
    ordinal = sense_ordinal(item.topic_id, course_map, knowledge=knowledge)
    return {1: "cause", 2: "rate", 3: "about"}.get(ordinal or 0, "other")


def _apply_frames(
    selected: list[PageCandidate],
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue,
    *,
    course_map: CourseMap | None = None,
    transcript: TranscriptDocument | None = None,
) -> list[PageCandidate]:
    used_frames: set[str] = set()
    used_assets: set[str] = set()
    assets = {asset.id: asset for asset in visual.assets}
    updated: list[PageCandidate] = []
    for item in selected:
        unit = _unit_by_id(knowledge, item.unit_id)
        if unit is None:
            updated.append(item)
            continue
        frames, layout = assign_frames(
            unit,
            visual,
            used_frames=used_frames,
            used_assets=used_assets,
            role=_candidate_role(item, knowledge, course_map),
            transcript=transcript,
        )
        for frame_id in frames:
            used_frames.add(frame_id)
            occurrence = next((row for row in visual.occurrences if row.id == frame_id), None)
            if occurrence is not None:
                used_assets.add(occurrence.asset_id)
                if occurrence.cluster_id:
                    used_assets.add(occurrence.cluster_id)
        item.frame_ids = frames
        item.layout = layout
        if layout == "text":
            item.selection_reason += "; text-only (no unused readable frame)"
        updated.append(item)
    del assets
    return updated


def _learner_cover_title(
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument | None,
    source_id: str,
) -> str:
    raw = course_map.topics[0].title.strip() if course_map and course_map.topics else ""
    if knowledge is not None and is_tsuki_grammar_lesson(course_map, knowledge):
        if not raw or _ENGLISH_INTRO_RE.search(raw):
            return "～につき"
    return (raw or "课程讲义")[:80] or source_id


def _cover_page(
    course_map: CourseMap | None,
    source_id: str,
    knowledge: KnowledgeDocument | None = None,
) -> PageIntent:
    title = _learner_cover_title(course_map, knowledge, source_id)
    return PageIntent(
        id="intent-cover",
        type="cover",
        title=title or source_id,
        claim_ids=[],
        frame_ids=[],
        notes="",
        selection_reason="封面",
        quality_label="draft",
    )


def _summary_units_for_topics(
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
    selected: list[PageCandidate],
) -> list[tuple[str, str, str]]:
    """Return compact, evidence-backed role descriptions and their real claims."""

    selected_by_topic: dict[str, list[PageCandidate]] = {}
    for item in selected:
        selected_by_topic.setdefault(item.topic_id, []).append(item)
    rows: list[tuple[str, str, str]] = []
    for ordinal in (1, 2, 3):
        topic = topic_for_sense_ordinal(course_map, ordinal, knowledge=knowledge)
        if topic is None:
            continue
        pool = [
            row
            for row in selected_by_topic.get(topic.id) or []
            if not _candidate_is_connective(row, knowledge, course_map)
        ]
        if not pool:
            continue
        picked = sorted(
            pool,
            key=lambda row: (0 if row.kind == "example" else 1, row.start_seconds, row.id),
        )[0]
        unit = _unit_by_id(knowledge, picked.unit_id)
        if unit is None:
            continue
        claim = next(
            (
                item for item in unit.claims
                if item.id in picked.claim_ids
                and item.provenance == "source"
                and item.evidence_ids
            ),
            None,
        )
        if claim is None:
            continue
        if ordinal == 1:
            bullet = "用法一：原因・理由（公告等）"
        elif ordinal == 2:
            bullet = "用法二：比例・単位（每个单位）"
        elif "について" in claim.text and re.search(
            r"通常|一般|普通|多数|多く|主に|常用|更常用",
            claim.text,
        ):
            bullet = "用法三：关于（罕用，通常用「について」）"
        else:
            bullet = "用法三：关于"
        rows.append(
            (
                sense_heading(ordinal),
                claim.id,
                bullet[:80],
            )
        )
    return rows


def _summary_page(
    selected: list[PageCandidate],
    *,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> PageIntent:
    rows = _summary_units_for_topics(course_map, knowledge, selected)
    claims = [claim_id for _, claim_id, _ in rows][:12]
    body_points = [bullet for _, _, bullet in rows][:4]
    if not claims:
        claims = [claim_id for item in selected for claim_id in item.claim_ids][:4]
        body_points = [sanitize_student_copy(item.title)[:80] for item in selected[:4]]
    return PageIntent(
        id="intent-summary",
        type="summary",
        title="本课总结",
        claim_ids=claims,
        frame_ids=[],
        notes="总结按课程主题覆盖各用法，并附代表性例句。",
        selection_reason="总结",
        quality_label="draft",
        body_points=body_points,
    )


def _content_page(
    item: PageCandidate,
    *,
    page_type: str = "content",
    course_map: CourseMap | None = None,
    knowledge: KnowledgeDocument | None = None,
) -> PageIntent:
    fallback = sanitize_student_copy(item.title)[:80] or item.title[:80]
    title = learner_page_title(
        topic_id=item.topic_id,
        course_map=course_map,
        fallback_title=fallback,
        knowledge=knowledge,
        unit_text=item.notes,
    )
    notes = item.notes
    if contains_student_meta(notes):
        cleaned = sanitize_student_copy(notes)
        notes = cleaned or " ".join(point for point in item.body_points if point.strip())[:4000]
    body_points = item.body_points
    unit = _unit_by_id(knowledge, item.unit_id) if knowledge is not None else None
    if (
        unit is not None
        and _is_usage_cause_page(item, course_map, knowledge)
        and item.kind == "example"
        and not item.selection_reason.startswith("source-backed cause examples;")
    ):
        body_points = _learner_bullets_for_claims(
            [claim for claim in unit.claims if claim.id in item.claim_ids]
        )
    return PageIntent(
        id=item.id,
        type=page_type,  # type: ignore[arg-type]
        layout=item.layout,
        title=title,
        claim_ids=item.claim_ids[:8],
        frame_ids=item.frame_ids[:3],
        notes=notes,
        selection_reason=item.selection_reason[:400],
        quality_label="draft",
        body_points=body_points[:4],
    )


def _trim_content_pages(
    content_pages: list[PageIntent],
    selected: list[PageCandidate],
    *,
    max_content: int,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[PageIntent]:
    if len(content_pages) <= max_content:
        return content_pages
    by_id = {page.id: page for page in content_pages}
    ordered = [by_id[item.id] for item in selected if item.id in by_id]
    selected_by_page = {item.id: item for item in selected}
    connective = topic_for_connective(course_map, knowledge)
    rate_about: list[PageIntent] = []
    cause_pages: list[tuple[PageCandidate, PageIntent]] = []
    connective_pages: list[PageIntent] = []
    optional: list[PageIntent] = []
    for page in ordered:
        item = selected_by_page.get(page.id)
        if item is None:
            optional.append(page)
            continue
        if _candidate_is_connective(item, knowledge, course_map) or (
            connective is not None and item.topic_id == connective.id and not _row_is_rate_only(item, knowledge)
        ):
            connective_pages.append(page)
            continue
        ordinal = _candidate_ordinal(item, course_map, knowledge)
        if ordinal in {2, 3}:
            rate_about.append(page)
        elif ordinal == 1:
            cause_pages.append((item, page))
        else:
            optional.append(page)
    ranked_cause = [
        page
        for _, page in sorted(cause_pages, key=lambda row: _cause_keep_key(row[0], knowledge))
    ]
    primary_cause = ranked_cause[:1]
    second_cause = ranked_cause[1:2]
    extra_cause = ranked_cause[2:]
    # 用法二/三, one 用法一, then 接续, then a second 用法一. Duplicate cause pages go last.
    protected = [*rate_about, *primary_cause, *connective_pages, *second_cause]
    if len(protected) > max_content:
        max_content = len(protected)
    body: list[PageIntent] = []
    for page in [*protected, *optional, *extra_cause]:
        if len(body) >= max_content:
            break
        if page not in body:
            body.append(page)
    if is_tsuki_grammar_lesson(course_map, knowledge):
        by_item = {item.id: item for item in selected}
        body.sort(
            key=lambda page: _pedagogical_rank(by_item[page.id], course_map, knowledge)
            if page.id in by_item
            else (9, 0.0, page.id)
        )
    else:
        order_map = {item.id: (item.start_seconds, item.id) for item in selected}
        body.sort(key=lambda page: order_map.get(page.id, (0.0, page.id)))
    return body


def _pedagogical_rank(
    item: PageCandidate,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> tuple[int, float, str]:
    """Sense order: intro, 接续, 用法一, 用法二, 用法三, then other pages."""

    if _candidate_is_connective(item, knowledge, course_map):
        return (1, item.start_seconds, item.id)
    if _row_is_rate_only(item, knowledge):
        return (3, item.start_seconds, item.id)
    topic = _topic_by_id(item.topic_id, course_map, knowledge)
    if topic is not None and is_grammar_connective_topic(topic, knowledge, course_map):
        return (1, item.start_seconds, item.id)
    ordinal = sense_ordinal(item.topic_id, course_map, knowledge=knowledge)
    if ordinal in {1, 2, 3}:
        return (1 + ordinal, item.start_seconds, item.id)
    if topic is not None and is_connective_topic(topic.title) and ordinal is None:
        return (0, item.start_seconds, item.id)
    return (5, item.start_seconds, item.id)


def _order_lesson_pages(
    selected: list[PageCandidate],
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
) -> list[PageCandidate]:
    if not is_tsuki_grammar_lesson(course_map, knowledge):
        return selected
    return sorted(
        selected,
        key=lambda item: _pedagogical_rank(item, course_map, knowledge),
    )


def _apply_pedagogical_page_order(
    plan: EditorialPlan,
    *,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
) -> EditorialPlan:
    """Put 接续 then 用法一/二/三 ahead of timestamp order. Cover and summary stay put."""

    if not is_tsuki_grammar_lesson(course_map, knowledge):
        return plan
    cover = [page for page in plan.pages if page.type == "cover"]
    summary = [page for page in plan.pages if page.type == "summary"]
    body = [page for page in plan.pages if page.type not in {"cover", "summary"}]

    def rank(page: PageIntent) -> tuple[int, float, str]:
        unit = _unit_for_claim_ids(knowledge, page.claim_ids)
        if unit is None:
            return (5, 0.0, page.id)
        topic = _topic_by_id(unit.topic_id, course_map, knowledge)
        if text_is_rate_only(_unit_display_text(unit)):
            group = 3
        elif unit_is_connective_attachment(unit, topic) or (
            topic is not None and is_grammar_connective_topic(topic, knowledge, course_map)
        ):
            group = 1
        else:
            ordinal = sense_ordinal(unit.topic_id, course_map, knowledge=knowledge)
            if ordinal in {1, 2, 3}:
                group = 1 + ordinal
            elif topic is not None and is_connective_topic(topic.title) and ordinal is None:
                group = 0
            else:
                group = 5
        return (group, unit.start_seconds, page.id)

    body.sort(key=rank)
    return plan.model_copy(update={"pages": [*cover, *body, *summary]})


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。．！？\n]", text or "") if part.strip()]


def _dedupe_lines(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        key = re.sub(r"\s+", "", line)
        if not key or key in seen:
            continue
        seen.add(key)
        kept.append(line)
    return kept


def _transcript_windows(transcript: TranscriptDocument) -> list[str]:
    """Join adjacent captions so a fee split across ASR segments can still match."""

    segments = sorted(transcript.segments, key=lambda item: (item.start_seconds, item.id))
    windows: list[str] = []
    buffer: list[str] = []
    buffer_end: float | None = None
    for segment in segments:
        if buffer and buffer_end is not None and segment.start_seconds - buffer_end > 2.0:
            windows.append(" ".join(buffer))
            buffer = []
        buffer.append(segment.text_original)
        buffer_end = segment.end_seconds
        if len(buffer) >= 4:
            windows.append(" ".join(buffer))
            buffer = []
            buffer_end = None
    if buffer:
        windows.append(" ".join(buffer))
    return windows


def _collect_rate_examples(
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue | None,
    transcript: TranscriptDocument | None,
) -> list[str]:
    texts = [claim.text for claim in knowledge.iter_claims()]
    if visual is not None:
        texts.extend(region.text for region in visual.ocr_regions)
    if transcript is not None:
        texts.extend(segment.text_original for segment in transcript.segments)
        texts.extend(_transcript_windows(transcript))
    return representative_rate_snippets(texts)


def _rate_claim_ids(knowledge: KnowledgeDocument) -> list[str]:
    ids: list[str] = []
    for claim in knowledge.iter_claims():
        if representative_rate_snippets([claim.text]):
            ids.append(claim.id)
    return ids


def _lines_without_rate(lines: list[str]) -> list[str]:
    kept: list[str] = []
    for line in lines:
        for sentence in _sentences(line) or [line]:
            cleaned = sanitize_student_copy(sentence).strip()
            if not cleaned or representative_rate_snippets([cleaned]):
                continue
            kept.append(cleaned[:200])
    return _dedupe_lines(kept)[:4]


def _short_about_bullet(text: str) -> str:
    cleaned = sanitize_student_copy(text)
    heading = sense_heading(3)
    body = cleaned
    if body.startswith(heading):
        body = body[len(heading) :].lstrip("：: ")
    parts = _sentences(body) or ([body.strip()] if body.strip() else [])
    examples = [
        part
        for part in parts
        if "について" in part and "中止形" not in part and len(part) <= 48
    ]
    chosen = examples[0] if examples else (min(parts, key=len) if parts else body)
    return summary_bullet_for_sense(3, chosen or body)[:200]


def _compact_rate_snippet(text: str) -> str:
    snippets = representative_rate_snippets([text])
    if snippets:
        return snippets[0]
    return text if len(text) <= 36 else text[:36]


def _rate_summary_bullet(examples: list[str]) -> str:
    snippets = [_compact_rate_snippet(text) for text in examples[:3]]
    snippets = _dedupe_lines(snippets)
    body = "；".join(snippets) if snippets else "比例・単位"
    return summary_bullet_for_sense(2, body)[:200]


def _about_pages_are_duplicates(left: PageIntent, right: PageIntent) -> bool:
    markers = ("使わない", "自衛隊", "についてが普通", "について が普通")

    def signature(page: PageIntent) -> tuple[str, ...]:
        text = " ".join([page.notes, *page.body_points])
        return tuple(marker for marker in markers if marker in text)

    left_sig = signature(left)
    right_sig = signature(right)
    if len(left_sig) >= 2 and left_sig == right_sig:
        return True
    left_text = re.sub(r"\s+", "", " ".join(left.body_points))
    right_text = re.sub(r"\s+", "", " ".join(right.body_points))
    if len(left_text) >= 16 and len(right_text) >= 16:
        return left_text in right_text or right_text in left_text
    return False


def _polish_learner_plan(
    plan: EditorialPlan,
    *,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None,
    visual: VisualCatalogue | None = None,
    transcript: TranscriptDocument | None = None,
) -> EditorialPlan:
    """Strip automation ids and keep proportion examples off 用法一."""

    pages: list[PageIntent] = []
    for page in plan.pages:
        title = page.title
        if page.type == "cover":
            title = _learner_cover_title(course_map, knowledge, plan.source_id)
        elif not is_fixed_sense_heading(title):
            title = sanitize_student_copy(title)[:80] or title
        notes = sanitize_student_copy(page.notes)
        body = [
            sanitize_student_copy(point)[:200]
            for point in page.body_points
            if sanitize_student_copy(point).strip()
        ]
        pages.append(page.model_copy(update={"title": title, "notes": notes, "body_points": body}))

    if not is_tsuki_grammar_lesson(course_map, knowledge):
        return plan.model_copy(update={"pages": pages})

    examples = _collect_rate_examples(knowledge, visual, transcript)
    rate_ids = [claim_id for claim_id in _rate_claim_ids(knowledge)]
    polished: list[PageIntent] = []
    for page in pages:
        if page.type == "content" and page.title.startswith("用法一"):
            body = _dedupe_lines(
                [
                    cleaned
                    for point in page.body_points
                    if (cleaned := sanitize_student_copy(point).strip())
                    and not representative_rate_snippets([cleaned])
                ]
            )[:4]
            notes = "。".join(body)[:4000]
            claim_ids = [claim_id for claim_id in page.claim_ids if claim_id not in rate_ids]
            if not claim_ids:
                claim_ids = list(page.claim_ids)
            polished.append(
                page.model_copy(
                    update={
                        "body_points": body or _lines_without_rate(page.body_points),
                        "notes": notes,
                        "claim_ids": claim_ids[:8],
                    }
                )
            )
            continue
        polished.append(page)

    rate_pages = [page for page in polished if page.type == "content" and page.title.startswith("用法二")]
    if rate_pages and examples:
        primary = rate_pages[0]
        merged_ids = list(dict.fromkeys([*rate_ids, *primary.claim_ids]))[:4]
        body = [line[:200] for line in examples][:4]
        updated = primary.model_copy(
            update={"claim_ids": merged_ids, "body_points": body, "notes": "。".join(body)[:4000]}
        )
        polished = [
            updated if page.id == primary.id else page
            for page in polished
            if page.id == primary.id or page not in rate_pages[1:]
        ]

    about_pages = [page for page in polished if page.type == "content" and page.title.startswith("用法三")]
    drop_ids: set[str] = set()
    if len(about_pages) > 1:
        keeper = min(about_pages, key=lambda page: (len(" ".join(page.body_points)), page.id))
        for page in about_pages:
            if page.id != keeper.id and _about_pages_are_duplicates(keeper, page):
                drop_ids.add(page.id)
        if not drop_ids:
            drop_ids = {page.id for page in about_pages if page.id != keeper.id}
    polished = [page for page in polished if page.id not in drop_ids]

    rewritten: list[PageIntent] = []
    for page in polished:
        if page.type != "summary":
            if page.type == "content" and page.title.startswith("用法三"):
                short = _short_about_bullet(" ".join(page.body_points) or page.notes)
                body = _dedupe_lines(page.body_points)[:4] or [short.split("：", 1)[-1][:200]]
                rewritten.append(
                    page.model_copy(update={"body_points": body, "notes": short[:4000]})
                )
                continue
            rewritten.append(page)
            continue
        points: list[str] = []
        for point in page.body_points:
            cleaned = sanitize_student_copy(point).strip()
            if cleaned:
                points.append(cleaned[:80])
        rewritten.append(
            page.model_copy(
                update={"body_points": _dedupe_lines(points)[:3] or page.body_points}
            )
        )
    return _apply_pedagogical_page_order(
        plan.model_copy(update={"pages": rewritten}),
        knowledge=knowledge,
        course_map=course_map,
    )


def build_deterministic_plan(
    selected: list[PageCandidate],
    *,
    omissions: list[Omission],
    source_id: str,
    target_pages: int,
    max_pages: int,
    order: DeckOrder,
    course_map: CourseMap | None,
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue | None = None,
    transcript: TranscriptDocument | None = None,
) -> EditorialPlan:
    pages = [_cover_page(course_map, source_id, knowledge)]
    displayed = _group_cause_candidates(
        selected,
        course_map=course_map,
        knowledge=knowledge,
    )
    displayed_claim_ids = {claim_id for item in displayed for claim_id in item.claim_ids}
    for item in selected:
        if not _is_usage_cause_page(item, course_map, knowledge):
            continue
        for claim_id in item.claim_ids:
            if claim_id not in displayed_claim_ids and not any(
                omission.claim_id == claim_id for omission in omissions
            ):
                omissions.append(
                    Omission(
                        topic_id=item.topic_id,
                        claim_id=claim_id,
                        reason="cause content exceeds the two-page learner-copy capacity; review required",
                    )
                )
    for item in _order_lesson_pages(displayed, course_map, knowledge):
        page_type = "quiz" if _is_practice(knowledge, item.claim_ids) else "content"
        pages.append(
            _content_page(
                item,
                page_type=page_type,
                course_map=course_map,
                knowledge=knowledge,
            )
        )
    if any(item.claim_ids for item in selected):
        pages.append(_summary_page(selected, course_map=course_map, knowledge=knowledge))
    if len(pages) > max_pages:
        body = _trim_content_pages(
            pages[1:-1],
            selected,
            max_content=max(0, max_pages - 2),
            course_map=course_map,
            knowledge=knowledge,
        )
        pages = [pages[0], *body, pages[-1]]
        if len(pages) > max_pages:
            pages = pages[:max_pages]
    summary_claims = {
        claim_id
        for page in pages
        if page.type == "summary"
        for claim_id in page.claim_ids
    }
    filtered_omissions = [
        item for item in omissions if item.claim_id not in summary_claims
    ]
    plan = EditorialPlan(
        schema_version="1.0",
        source_id=source_id,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
        pages=pages,
        omissions=filtered_omissions,
    )
    restored = _restore_sense_content_titles(
        plan,
        knowledge=knowledge,
        course_map=course_map,
    )
    return _polish_learner_plan(
        restored,
        knowledge=knowledge,
        course_map=course_map,
        visual=visual,
        transcript=transcript,
    )


def _is_practice(knowledge: KnowledgeDocument, claim_ids: list[str]) -> bool:
    claims = {claim.id: claim for claim in knowledge.iter_claims()}
    chosen = [claims[item] for item in claim_ids if item in claims]
    return bool(chosen) and all(claim.provenance == "generated-practice" for claim in chosen)


def allowed_editor_ids(
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue,
) -> tuple[set[str], set[str]]:
    claims = {claim.id for claim in knowledge.iter_claims()}
    claims.update(unit.id for unit in knowledge.units)
    frames = {occurrence.id for occurrence in visual.occurrences}
    return claims, frames


def validate_editorial_payload(
    structured: dict[str, Any] | None,
    *,
    allowed_claim_ids: set[str],
    allowed_frame_ids: set[str],
    max_pages: int,
    source_id: str,
    target_pages: int,
    order: DeckOrder,
    required_page_ids: set[str] | None = None,
    required_claim_ids: set[str] | None = None,
) -> EditorialPlan:
    if not isinstance(structured, dict):
        raise EditorContractError("editor result missing structured object")
    raw_pages = structured.get("pages")
    if not isinstance(raw_pages, list) or not raw_pages:
        raise EditorContractError("editor result must contain a pages list")
    pages: list[PageIntent] = []
    for raw in raw_pages:
        if not isinstance(raw, dict):
            raise EditorContractError("editorial page must be an object")
        try:
            page = PageIntent.model_validate(raw)
        except ValidationError as error:
            raise EditorContractError(f"editorial page failed schema: {error}") from error
        unknown_claims = [item for item in page.claim_ids if item not in allowed_claim_ids]
        if unknown_claims:
            raise EditorContractError(f"unknown claim ids {unknown_claims}")
        unknown_frames = [item for item in page.frame_ids if item not in allowed_frame_ids]
        if unknown_frames:
            raise EditorContractError(f"unknown frame ids {unknown_frames}")
        for value in (page.title, page.notes, page.selection_reason, *page.body_points, *page.claim_ids, *page.frame_ids):
            if contains_path_literal(value):
                raise EditorContractError("editorial copy contains a bare path")
        pages.append(page)
    if len(pages) > max_pages:
        raise EditorContractError("editorial pages exceed max_pages")
    page_ids = {page.id for page in pages}
    if required_page_ids and required_page_ids - page_ids:
        raise EditorContractError(
            f"model dropped required coverage {sorted(required_page_ids - page_ids)}"
        )
    selected_claims = {claim_id for page in pages for claim_id in page.claim_ids}
    if required_claim_ids and required_claim_ids - selected_claims:
        raise EditorContractError("model dropped the selected/omitted claim partition")
    omissions: list[Omission] = []
    for raw in structured.get("omissions") or []:
        if isinstance(raw, dict):
            try:
                omissions.append(Omission.model_validate(raw))
            except ValidationError as error:
                raise EditorContractError(f"omission failed schema: {error}") from error
    return EditorialPlan(
        schema_version="1.0",
        source_id=source_id,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
        pages=pages,
        omissions=omissions,
    )


def apply_model_organization(
    fallback: EditorialPlan,
    structured: dict[str, Any] | None,
    *,
    allowed_claim_ids: set[str],
    allowed_frame_ids: set[str],
    order: DeckOrder,
) -> EditorialPlan:
    """Allow the model to rewrite copy/order only for the deterministic pages."""

    required_ids = {page.id for page in fallback.pages}
    required_claims = {claim_id for page in fallback.pages for claim_id in page.claim_ids}
    planned = validate_editorial_payload(
        structured,
        allowed_claim_ids=allowed_claim_ids,
        allowed_frame_ids=allowed_frame_ids,
        max_pages=fallback.max_pages,
        source_id=fallback.source_id,
        target_pages=fallback.target_pages,
        order=order,
        required_page_ids=required_ids,
        required_claim_ids=required_claims,
    )
    fallback_by_id = {page.id: page for page in fallback.pages}
    model_by_id = {page.id: page for page in planned.pages}
    extra = set(model_by_id) - required_ids
    if extra:
        raise EditorContractError(f"model added pages outside the selected partition {sorted(extra)}")
    cover = next(page for page in fallback.pages if page.type == "cover")
    summary = next((page for page in fallback.pages if page.type == "summary"), None)
    body = [page for page in fallback.pages if page.id not in {cover.id, *( [summary.id] if summary else [])}]
    model_body_order = [
        page.id
        for page in planned.pages
        if page.id not in {cover.id, *( [summary.id] if summary else [])}
    ]
    if set(model_body_order) != {page.id for page in body}:
        raise EditorContractError("model changed the selected page partition")
    if order == "chronological":
        model_body_order = [page.id for page in body]

    def overlay(base: PageIntent, incoming: PageIntent) -> PageIntent:
        incoming_title = sanitize_student_copy(incoming.title)[:80].strip()
        if is_fixed_sense_heading(base.title):
            title = base.title
        elif incoming_title:
            title = incoming_title
        else:
            title = base.title
        if base.type == "summary":
            body_points = list(base.body_points)
        else:
            body_points = [
                sanitize_student_copy(point)[:200]
                for point in incoming.body_points
                if sanitize_student_copy(point).strip()
            ] or base.body_points
            if base.selection_reason.startswith("source-backed cause examples;"):
                body_points = list(base.body_points)
        return base.model_copy(
            update={
                "title": title,
                "notes": incoming.notes,
                "body_points": body_points,
            }
        )

    merged = [overlay(cover, model_by_id[cover.id])]
    for page_id in model_body_order:
        merged.append(overlay(fallback_by_id[page_id], model_by_id[page_id]))
    if summary is not None:
        merged.append(overlay(summary, model_by_id[summary.id]))
    return fallback.model_copy(update={"pages": merged, "omissions": list(fallback.omissions)})


def edit_deck(
    knowledge: KnowledgeDocument,
    *,
    course_map: CourseMap | None = None,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider,
    target_pages: int = 12,
    max_pages: int = 20,
    order: DeckOrder = "chronological",
    quality_label: PageQualityLabel = "draft",
    cancel_event: Event | None = None,
) -> EditorialPlan:
    """Score coverage first, then let the model organize titles/order/copy."""

    if max_pages < target_pages:
        raise EditorContractError("max_pages must be >= target_pages")
    scored = score_candidates(knowledge, visual=visual, course_map=course_map)
    valid_evidence_ids = {
        *(segment.id for segment in transcript.segments),
        *(occurrence.id for occurrence in visual.occurrences),
        *(region.id for region in visual.ocr_regions),
    }
    selected, omissions = select_candidates(
        scored,
        target_pages=target_pages,
        max_pages=max_pages,
        knowledge=knowledge,
        course_map=course_map,
        valid_evidence_ids=valid_evidence_ids,
    )
    selected = sort_candidates(selected, order=order, knowledge=knowledge)
    selected = _apply_frames(
        selected,
        knowledge,
        visual,
        course_map=course_map,
        transcript=transcript,
    )
    fallback = build_deterministic_plan(
        selected,
        omissions=omissions,
        source_id=knowledge.source_id,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
        course_map=course_map,
        knowledge=knowledge,
        visual=visual,
        transcript=transcript,
    )
    allowed_claims, allowed_frames = allowed_editor_ids(knowledge, visual)
    payload = {
        "prompt": load_prompt("editor.md"),
        "target_pages": target_pages,
        "max_pages": max_pages,
        "order": order,
        "course_title": course_map.topics[0].title if course_map and course_map.topics else knowledge.source_id,
        "candidates": [
            {
                "id": item.id,
                "unit_id": item.unit_id,
                "topic_id": item.topic_id,
                "kind": item.kind,
                "claim_ids": item.claim_ids,
                "frame_ids": item.frame_ids,
                "layout": item.layout,
                "selection_reason": item.selection_reason,
                "start_seconds": item.start_seconds,
            }
            for item in selected
        ],
        "omissions": [item.model_dump(mode="json") for item in omissions],
        "allowed_claim_ids": sorted(allowed_claims),
        "allowed_frame_ids": sorted(allowed_frames),
        "constraints": {"external_knowledge": False, "page_budget": max_pages},
    }
    request = model_request(
        request_id=f"editor:plan:{order}:{target_pages}:{max_pages}",
        role="editor",
        payload=payload,
    )
    attach_provider_payload(provider, payload)
    result = provider.complete(request, cancel_event=cancel_event)
    try:
        planned = apply_model_organization(
            fallback,
            result.structured,
            allowed_claim_ids=allowed_claims,
            allowed_frame_ids=allowed_frames,
            order=order,
        )
    except (EditorContractError, ValidationError):
        planned = fallback
    planned = _restore_sense_content_titles(
        planned,
        knowledge=knowledge,
        course_map=course_map,
    )
    planned = _apply_pedagogical_page_order(
        planned,
        knowledge=knowledge,
        course_map=course_map,
    )
    planned = _polish_learner_plan(
        planned,
        knowledge=knowledge,
        course_map=course_map,
        visual=visual,
        transcript=transcript,
    )
    labeled = []
    for page in planned.pages:
        labeled.append(relabel_page(page, quality_label) if quality_label != "verified" else page)
    return planned.model_copy(update={"pages": labeled, "order": order})
