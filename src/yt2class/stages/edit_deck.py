"""Global editor: deterministic coverage scoring, then LLM titles/order/copy."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Any, Iterable

from pydantic import ValidationError

from yt2class.adapters.providers.base import Provider
from yt2class.domain.course_map import CourseMap
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
from yt2class.stages.llm_util import contains_path_literal, load_prompt, model_request

KIND_IMPORTANCE = {
    "concept": 1.0,
    "warning": 0.95,
    "procedure": 0.92,
    "comparison": 0.88,
    "example": 0.55,
    "recap": 0.22,
}


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


def _unit_for_claim(knowledge: KnowledgeDocument, claim_id: str) -> KnowledgeUnit | None:
    for unit in knowledge.units:
        if any(claim.id == claim_id for claim in unit.claims):
            return unit
    return None


def _unit_by_id(knowledge: KnowledgeDocument, unit_id: str) -> KnowledgeUnit | None:
    return next((unit for unit in knowledge.units if unit.id == unit_id), None)


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


def _score_unit(unit: KnowledgeUnit) -> tuple[float, str]:
    importance = KIND_IMPORTANCE.get(unit.kind, 0.5)
    evidence = min(1.0, sum(len(claim.evidence_ids) for claim in unit.claims) / 3.0)
    topic_gain = 1.0
    new_info = 1.0
    redundancy = 0.0
    score = (
        0.35 * topic_gain
        + 0.25 * importance
        + 0.20 * evidence
        + 0.20 * new_info
        - 0.30 * redundancy
    )
    reason = (
        f"coverage={topic_gain:.2f} importance={importance:.2f} "
        f"evidence={evidence:.2f} novelty={new_info:.2f}"
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


def assign_frames(
    unit: KnowledgeUnit,
    visual: VisualCatalogue,
    *,
    used_frames: set[str],
    used_assets: set[str],
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
        scored.append((_frame_score(unit, occurrence, by_candidate.get(frame_id)) - penalty, frame_id))
    scored.sort(key=lambda item: (-item[0], item[1]))
    wanted = 2 if unit.kind == "comparison" else 3 if unit.kind == "procedure" else 1
    picked: list[str] = []
    for _score, frame_id in scored:
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
        title = (unit.claims[0].text if unit.claims else unit.kind)[:80]
        notes = " ".join(claim.text for claim in unit.claims)[:4000]
        body = [claim.text[:200] for claim in unit.claims[:4]]
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


def _example_keep(candidates: Iterable[PageCandidate]) -> set[str]:
    best: dict[str, PageCandidate] = {}
    for item in candidates:
        if item.kind != "example":
            continue
        current = best.get(item.topic_id)
        if current is None or item.score > current.score:
            best[item.topic_id] = item
    return {item.unit_id for item in best.values()}


def select_candidates(
    candidates: list[PageCandidate],
    *,
    target_pages: int,
    max_pages: int,
    knowledge: KnowledgeDocument,
    course_map: CourseMap | None = None,
) -> tuple[list[PageCandidate], list[Omission]]:
    del course_map
    content_target = max(1, min(target_pages, max_pages) - 2)
    content_max = max(1, max_pages - 2)
    parents = prerequisite_parents(knowledge)
    keep_examples = _example_keep(candidates)
    ranked = sorted(candidates, key=lambda item: (-item.score, item.start_seconds, item.id))
    selected: list[PageCandidate] = []
    selected_ids: set[str] = set()

    def add(item: PageCandidate) -> None:
        if item.unit_id in selected_ids:
            return
        if len(selected) >= content_max:
            return
        selected.append(item)
        selected_ids.add(item.unit_id)

    for item in ranked:
        if item.kind == "example" and item.unit_id not in keep_examples:
            continue
        for parent_id in sorted(parents.get(item.unit_id, set())):
            parent = next((row for row in ranked if row.unit_id == parent_id), None)
            if parent is not None:
                add(parent)
        if len(selected) >= content_target and item.unit_id not in selected_ids:
            if item.required_prerequisite:
                add(item)
            continue
        add(item)

    selected_ids = {item.unit_id for item in selected}
    omissions: list[Omission] = []
    for item in candidates:
        if item.unit_id in selected_ids:
            continue
        for claim_id in item.claim_ids:
            omissions.append(
                Omission(
                    topic_id=item.topic_id,
                    claim_id=claim_id,
                    reason="page budget or representative-example compression",
                )
            )
    return selected, omissions


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


def _apply_frames(
    selected: list[PageCandidate],
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue,
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
            unit, visual, used_frames=used_frames, used_assets=used_assets
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


def _cover_page(course_map: CourseMap | None, source_id: str) -> PageIntent:
    title = "课程讲义"
    if course_map and course_map.topics:
        title = course_map.topics[0].title[:80]
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


def _summary_page(selected: list[PageCandidate]) -> PageIntent:
    claims = [claim_id for item in selected for claim_id in item.claim_ids][:8]
    return PageIntent(
        id="intent-summary",
        type="summary",
        title="本课总结",
        claim_ids=claims,
        frame_ids=[],
        notes="总结复用已选 claim。",
        selection_reason="总结",
        quality_label="draft",
        body_points=[item.title[:200] for item in selected[:4]],
    )


def _content_page(item: PageCandidate, *, page_type: str = "content") -> PageIntent:
    return PageIntent(
        id=item.id,
        type=page_type,  # type: ignore[arg-type]
        layout=item.layout,
        title=item.title[:80],
        claim_ids=item.claim_ids[:8],
        frame_ids=item.frame_ids[:3],
        notes=item.notes,
        selection_reason=item.selection_reason[:400],
        quality_label="draft",
        body_points=item.body_points[:4],
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
) -> EditorialPlan:
    pages = [_cover_page(course_map, source_id)]
    for item in selected:
        page_type = "quiz" if _is_practice(knowledge, item.claim_ids) else "content"
        pages.append(_content_page(item, page_type=page_type))
    pages.append(_summary_page(selected))
    if len(pages) > max_pages:
        body = pages[1:-1][: max(0, max_pages - 2)]
        pages = [pages[0], *body, pages[-1]]
        if len(pages) > max_pages:
            pages = pages[:max_pages]
    return EditorialPlan(
        schema_version="1.0",
        source_id=source_id,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
        pages=pages,
        omissions=omissions,
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


def _attach_payload(provider: Provider, payload: dict[str, Any]) -> None:
    if hasattr(provider, "last_payload"):
        provider.last_payload = payload


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

    del transcript
    if max_pages < target_pages:
        raise EditorContractError("max_pages must be >= target_pages")
    scored = score_candidates(knowledge, visual=visual, course_map=course_map)
    selected, omissions = select_candidates(
        scored,
        target_pages=target_pages,
        max_pages=max_pages,
        knowledge=knowledge,
        course_map=course_map,
    )
    selected = sort_candidates(selected, order=order, knowledge=knowledge)
    selected = _apply_frames(selected, knowledge, visual)
    fallback = build_deterministic_plan(
        selected,
        omissions=omissions,
        source_id=knowledge.source_id,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
        course_map=course_map,
        knowledge=knowledge,
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
                "title": item.title,
                "notes": item.notes,
                "body_points": item.body_points,
                "selection_reason": item.selection_reason,
                "start_seconds": item.start_seconds,
            }
            for item in selected
        ],
        "selected": [
            {
                "id": item.id,
                "claim_ids": item.claim_ids,
                "frame_ids": item.frame_ids,
                "layout": item.layout,
                "title": item.title,
                "notes": item.notes,
                "body_points": item.body_points,
                "selection_reason": item.selection_reason,
            }
            for item in selected
        ],
        "omissions": [item.model_dump(mode="json") for item in omissions],
        "allowed_claim_ids": sorted(allowed_claims),
        "allowed_frame_ids": sorted(allowed_frames),
        "constraints": {"external_knowledge": False, "page_budget": max_pages},
    }
    request = model_request(request_id="editor:plan", role="editor", payload=payload)
    _attach_payload(provider, payload)
    result = provider.complete(request, cancel_event=cancel_event)
    try:
        planned = validate_editorial_payload(
            result.structured,
            allowed_claim_ids=allowed_claims,
            allowed_frame_ids=allowed_frames,
            max_pages=max_pages,
            source_id=knowledge.source_id,
            target_pages=target_pages,
            order=order,
        )
    except (EditorContractError, ValidationError):
        planned = fallback
    labeled = []
    for page in planned.pages:
        labeled.append(relabel_page(page, quality_label) if quality_label != "verified" else page)
    return planned.model_copy(update={"pages": labeled, "order": order})
