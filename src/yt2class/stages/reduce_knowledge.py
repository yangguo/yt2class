"""Merge duplicate knowledge units without collapsing contradictions."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

from yt2class.domain.course_map import CourseMap, forbid_speculative_source_claims
from yt2class.domain.knowledge import (
    KnowledgeClaim,
    KnowledgeDocument,
    KnowledgeRelation,
    KnowledgeUnit,
    Uncertainty,
)

NEGATION_RE = re.compile(
    r"(不是|不会|不能|不要|并非|并未|没有|从未|绝不|\bnot\b|\bnever\b|\bno\b|n't)",
    re.I,
)
WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+")


class KnowledgeReduceError(ValueError):
    """Raised when reduced relations violate procedure order."""


def strip_negation(text: str) -> str:
    return NEGATION_RE.sub(" ", text)


def polarity(text: str) -> str:
    return "neg" if NEGATION_RE.search(text) else "pos"


def normalize_concept(text: str) -> str:
    cleaned = strip_negation(text).lower()
    cleaned = re.sub(r"[是的了與与和]", "", cleaned)
    tokens = WORD_RE.findall(cleaned)
    return "".join(tokens)


def concept_key(claim: KnowledgeClaim) -> str:
    qualifiers = ",".join(sorted(item.strip().lower() for item in claim.qualifiers if item.strip()))
    return f"{normalize_concept(claim.text)}|{qualifiers}|{polarity(claim.text)}"


def sense_key(claim: KnowledgeClaim) -> str:
    """Same surface word with different qualifiers is a different sense."""

    return f"{normalize_concept(claim.text)}|{','.join(sorted(claim.qualifiers))}"


def time_overlap(left: KnowledgeUnit, right: KnowledgeUnit) -> bool:
    return left.end_seconds > right.start_seconds and right.end_seconds > left.start_seconds


def evidence_overlap(left: KnowledgeClaim, right: KnowledgeClaim) -> bool:
    return bool(set(left.evidence_ids) & set(right.evidence_ids))


def claims_contradict(left: KnowledgeClaim, right: KnowledgeClaim) -> bool:
    if normalize_concept(left.text) != normalize_concept(right.text):
        return False
    if set(left.qualifiers) != set(right.qualifiers):
        return False
    return polarity(left.text) != polarity(right.text)


def claims_equivalent(left: KnowledgeClaim, right: KnowledgeClaim) -> bool:
    """Evidence overlap can support dedupe only after concepts already match."""

    if claims_contradict(left, right):
        return False
    if sense_key(left) != sense_key(right):
        return False
    return concept_key(left) == concept_key(right)


def should_merge_units(left: KnowledgeUnit, right: KnowledgeUnit) -> bool:
    if left.kind == "recap" or right.kind == "recap":
        return False
    if left.kind != right.kind:
        return False
    if not time_overlap(left, right):
        return False
    if any(claims_contradict(a, b) for a in left.claims for b in right.claims):
        return False
    return any(claims_equivalent(a, b) for a in left.claims for b in right.claims)


def _merge_claim_pair(left: KnowledgeClaim, right: KnowledgeClaim) -> KnowledgeClaim:
    evidence = list(dict.fromkeys([*left.evidence_ids, *right.evidence_ids]))
    qualifiers = list(dict.fromkeys([*left.qualifiers, *right.qualifiers]))
    status = left.status
    if "unresolved" in {left.status, right.status}:
        status = "unresolved"
    elif "contradicted" in {left.status, right.status}:
        status = "contradicted"
    return left.model_copy(update={"evidence_ids": evidence[:20], "qualifiers": qualifiers[:20], "status": status})


def merge_unit_pair(left: KnowledgeUnit, right: KnowledgeUnit) -> KnowledgeUnit:
    claims: list[KnowledgeClaim] = []
    used_right: set[str] = set()
    for claim in left.claims:
        match = next(
            (
                other
                for other in right.claims
                if other.id not in used_right and claims_equivalent(claim, other)
            ),
            None,
        )
        if match is None:
            claims.append(claim)
            continue
        used_right.add(match.id)
        claims.append(_merge_claim_pair(claim, match))
    claims.extend(other for other in right.claims if other.id not in used_right)

    relations = list(left.relations)
    seen = {(item.from_id, item.to_id, item.kind) for item in relations}
    for item in right.relations:
        key = (item.from_id, item.to_id, item.kind)
        if key not in seen:
            relations.append(item)
            seen.add(key)

    visuals = list(left.visual_candidates)
    seen_frames = {item.frame_id for item in visuals}
    for item in right.visual_candidates:
        if item.frame_id not in seen_frames:
            visuals.append(item)
            seen_frames.add(item.frame_id)

    uncertainty = list(left.uncertainty) + list(right.uncertainty)
    requests = list(left.evidence_requests) + list(right.evidence_requests)
    return left.model_copy(
        update={
            "segment_ids": list(dict.fromkeys([*left.segment_ids, *right.segment_ids]))[:20],
            "start_seconds": min(left.start_seconds, right.start_seconds),
            "end_seconds": max(left.end_seconds, right.end_seconds),
            "claims": claims,
            "relations": relations,
            "visual_candidates": visuals,
            "uncertainty": uncertainty,
            "evidence_requests": requests,
        }
    )


def _merge_group(units: list[KnowledgeUnit]) -> list[KnowledgeUnit]:
    remaining = list(units)
    changed = True
    while changed:
        changed = False
        merged: list[KnowledgeUnit] = []
        skip: set[int] = set()
        for i, left in enumerate(remaining):
            if i in skip:
                continue
            current = left
            for j, right in enumerate(remaining[i + 1 :], start=i + 1):
                if j in skip:
                    continue
                if should_merge_units(current, right):
                    current = merge_unit_pair(current, right)
                    skip.add(j)
                    changed = True
            merged.append(current)
        remaining = merged
    return remaining


def _step_claims(unit: KnowledgeUnit) -> list[KnowledgeClaim]:
    return list(unit.claims)


STEP_INDEX = re.compile(r"(?:步骤\s*|step\s+)(\d+)", re.I)


def _concept_text_overlap(left: KnowledgeUnit, right: KnowledgeUnit) -> bool:
    left_keys = {normalize_concept(claim.text) for claim in left.claims if normalize_concept(claim.text)}
    right_keys = {normalize_concept(claim.text) for claim in right.claims if normalize_concept(claim.text)}
    for first in left_keys:
        for second in right_keys:
            if first == second or first in second or second in first:
                return True
    return False


def _step_indexes(unit: KnowledgeUnit) -> list[int]:
    found: list[int] = []
    for claim in unit.claims:
        match = STEP_INDEX.search(claim.text)
        if match:
            found.append(int(match.group(1)))
    return found


def _justified_sequence(left: KnowledgeUnit, right: KnowledgeUnit, *, kind: str) -> bool:
    """True only when a cross-unit edge is supported by procedure/topic overlap.

    Shared evidence alone is not enough: an incidental shared frame or caption
    must not invent step_before or prerequisite links.
    """

    shared_topic = bool(left.topic_id and left.topic_id == right.topic_id)
    if not shared_topic:
        return False
    if kind == "step_before":
        left_steps = _step_indexes(left)
        right_steps = _step_indexes(right)
        numbered = bool(left_steps and right_steps and min(right_steps) == max(left_steps) + 1)
        return numbered or _concept_text_overlap(left, right)
    if kind == "prerequisite":
        return _concept_text_overlap(left, right)
    return False


def link_cross_segment_relations(units: list[KnowledgeUnit]) -> list[KnowledgeUnit]:
    """Connect prerequisite, comparison, and procedure steps across windows."""

    by_id = {unit.id: unit for unit in units}
    extras: dict[str, list[KnowledgeRelation]] = {unit.id: [] for unit in units}

    procedures = [unit for unit in units if unit.kind == "procedure"]
    procedures.sort(key=lambda unit: (unit.start_seconds, unit.id))
    for previous, current in zip(procedures, procedures[1:]):
        if not _justified_sequence(previous, current, kind="step_before"):
            continue
        extras[previous.id].append(
            KnowledgeRelation(from_id=previous.id, to_id=current.id, kind="step_before")
        )

    recaps = [unit for unit in units if unit.kind == "recap"]
    sources = [unit for unit in units if unit.kind != "recap"]
    for recap in recaps:
        for source in sources:
            if normalize_concept(" ".join(claim.text for claim in recap.claims)) and any(
                normalize_concept(claim.text) in normalize_concept(other.text)
                or normalize_concept(other.text) in normalize_concept(claim.text)
                for claim in recap.claims
                for other in source.claims
            ):
                extras[source.id].append(
                    KnowledgeRelation(from_id=source.id, to_id=recap.id, kind="supports")
                )
                break

    comparisons = [unit for unit in units if unit.kind == "comparison"]
    for left, right in zip(comparisons, comparisons[1:]):
        extras[left.id].append(
            KnowledgeRelation(from_id=left.id, to_id=right.id, kind="contrasts")
        )

    concepts = [unit for unit in units if unit.kind == "concept"]
    concepts.sort(key=lambda unit: unit.start_seconds)
    for previous, current in zip(concepts, concepts[1:]):
        if not _justified_sequence(previous, current, kind="prerequisite"):
            continue
        extras[previous.id].append(
            KnowledgeRelation(from_id=previous.id, to_id=current.id, kind="prerequisite")
        )

    updated: list[KnowledgeUnit] = []
    for unit in units:
        relations = list(unit.relations)
        seen = {(item.from_id, item.to_id, item.kind) for item in relations}
        for item in extras.get(unit.id, []):
            key = (item.from_id, item.to_id, item.kind)
            if key not in seen:
                relations.append(item)
                seen.add(key)
        updated.append(unit.model_copy(update={"relations": relations}))
        by_id[unit.id] = updated[-1]
    return updated


def validate_procedure_order(units: Iterable[KnowledgeUnit]) -> list[str]:
    problems: list[str] = []
    procedures = [unit for unit in units if unit.kind == "procedure"]
    lookup = {unit.id: unit for unit in units}
    claim_times = {
        claim.id: unit.start_seconds for unit in units for claim in unit.claims
    }
    for unit in procedures:
        for relation in unit.relations:
            if relation.kind != "step_before":
                continue
            start = lookup.get(relation.from_id)
            end = lookup.get(relation.to_id)
            from_time = start.start_seconds if start else claim_times.get(relation.from_id)
            to_time = end.start_seconds if end else claim_times.get(relation.to_id)
            if from_time is None or to_time is None:
                continue
            if to_time + 1e-9 < from_time:
                problems.append(
                    f"procedure order inverted: {relation.from_id} -> {relation.to_id}"
                )
    numbered = []
    for unit in procedures:
        for claim in unit.claims:
            match = re.search(r"(?:步骤\s*)?(\d+)", claim.text)
            if match:
                numbered.append((int(match.group(1)), unit, claim))
    numbered.sort(key=lambda item: item[0])
    if numbered:
        values = [item[0] for item in numbered]
        expected = list(range(values[0], values[-1] + 1))
        missing = [item for item in expected if item not in values]
        if missing:
            problems.append(f"procedure missing steps {missing}")
    return problems


def retain_conflicts(units: list[KnowledgeUnit]) -> list[KnowledgeUnit]:
    updated = list(units)
    for i, left in enumerate(updated):
        extra: list[Uncertainty] = []
        for right in updated[i + 1 :]:
            for a in left.claims:
                for b in right.claims:
                    if claims_contradict(a, b):
                        extra.append(
                            Uncertainty(
                                kind="conflict",
                                start_seconds=min(left.start_seconds, right.start_seconds),
                                end_seconds=max(left.end_seconds, right.end_seconds),
                                note=f"source conflict between {a.id} and {b.id}",
                            )
                        )
        if extra:
            updated[i] = left.model_copy(update={"uncertainty": [*left.uncertainty, *extra]})
    return updated


@dataclass(frozen=True)
class ReduceResult:
    document: KnowledgeDocument
    problems: list[str]


def _owning_relations(units: list[KnowledgeUnit]) -> list[KnowledgeUnit]:
    """KnowledgeDocument only allows from_id to be a claim or the owning unit."""

    by_id = {unit.id: unit for unit in units}
    claim_ids = {claim.id for unit in units for claim in unit.claims}
    extras: dict[str, list[KnowledgeRelation]] = {unit.id: [] for unit in units}
    cleaned: list[KnowledgeUnit] = []
    for unit in units:
        keep: list[KnowledgeRelation] = []
        for relation in unit.relations:
            if relation.from_id == unit.id or relation.from_id in claim_ids:
                keep.append(relation)
            elif relation.from_id in by_id:
                extras[relation.from_id].append(relation)
            else:
                keep.append(relation)
        cleaned.append(unit.model_copy(update={"relations": keep}))
    relocated: list[KnowledgeUnit] = []
    for unit in cleaned:
        relations = list(unit.relations)
        seen = {(item.from_id, item.to_id, item.kind) for item in relations}
        for item in extras.get(unit.id, []):
            key = (item.from_id, item.to_id, item.kind)
            if key not in seen:
                relations.append(item)
                seen.add(key)
        relocated.append(unit.model_copy(update={"relations": relations}))
    return relocated


def reduce_knowledge(
    units: list[KnowledgeUnit],
    *,
    source_id: str,
    course_map: CourseMap | None = None,
) -> KnowledgeDocument:
    merged = _merge_group(units)
    merged = retain_conflicts(merged)
    merged = link_cross_segment_relations(merged)
    merged = _owning_relations(merged)
    problems = validate_procedure_order(merged)
    if problems:
        annotated: list[KnowledgeUnit] = []
        for unit in merged:
            if unit.kind != "procedure":
                annotated.append(unit)
                continue
            notes = [
                Uncertainty(
                    kind="missing_step",
                    start_seconds=unit.start_seconds,
                    end_seconds=unit.end_seconds,
                    note=problem[:400],
                )
                for problem in problems
            ]
            annotated.append(unit.model_copy(update={"uncertainty": [*unit.uncertainty, *notes]}))
        merged = annotated

    if course_map is not None:
        speculative = {topic.id for topic in course_map.topics if topic.speculative}
        guarded: list[KnowledgeUnit] = []
        for unit in merged:
            if unit.topic_id not in speculative:
                guarded.append(unit)
                continue
            claims = [
                claim.model_copy(update={"status": "unresolved", "provenance": "source"})
                if claim.provenance == "source" and claim.status in {"supported", "draft"}
                else claim
                for claim in unit.claims
            ]
            # Unresolved sourced-looking claims on a speculative topic are still forbidden
            # if they remain provenance=source + draft/supported. Force insufficient.
            claims = [
                claim.model_copy(update={"status": "insufficient", "qualifiers": [*claim.qualifiers, "speculative-topic"]})
                if claim.provenance == "source"
                else claim
                for claim in claims
            ]
            guarded.append(unit.model_copy(update={"claims": claims}))
        merged = guarded

    # Re-check after guarding
    if course_map is not None:
        forbid_speculative_source_claims(
            course_map,
            [
                type(
                    "ClaimView",
                    (),
                    {
                        "id": claim.id,
                        "topic_id": unit.topic_id,
                        "provenance": claim.provenance,
                        "status": claim.status,
                    },
                )()
                for unit in merged
                for claim in unit.claims
            ],
        )

    return KnowledgeDocument(schema_version="1.0", source_id=source_id, units=merged)
