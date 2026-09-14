"""Hybrid routing: upgrade to native video only when frames / verifier are insufficient."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from yt2class.adapters.providers.base import ProviderCapabilities
from yt2class.adapters.providers.native_video import NativeVideoAdapter, NativeVideoError
from yt2class.domain.course_map import CourseMap
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeUnit
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.segment import AnalysisWindow
from yt2class.domain.slide_spec_v3 import AnalysisMode
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue

if TYPE_CHECKING:
    from yt2class.stages.analyze_segments import SegmentAnalysisOutcome
    from yt2class.stages.evidence_refinement import RefinementBudget

MIN_CLIP_SECONDS = 5.0
MAX_CLIP_SECONDS = 30.0
RAPID_SCENE_SECONDS = 3.0


def rapid_scene_changes(visual: VisualCatalogue, start: float, end: float) -> bool:
    scenes = [
        scene
        for scene in visual.scenes
        if scene.end_seconds > start and scene.start_seconds < end
    ]
    if len(scenes) < 3:
        return False
    durations = [max(0.0, scene.end_seconds - scene.start_seconds) for scene in scenes]
    if not durations:
        return False
    average = sum(durations) / len(durations)
    return average < RAPID_SCENE_SECONDS


def claim_needs_verifier_upgrade(claim: KnowledgeClaim) -> bool:
    return claim.status in {"insufficient", "unresolved", "contradicted"}


def unit_needs_native_upgrade(
    unit: KnowledgeUnit,
    *,
    visual: VisualCatalogue,
    verifier_insufficient: bool = False,
) -> bool:
    if unit.kind == "procedure":
        if unit.evidence_requests or unit.uncertainty:
            return True
        if any(claim_needs_verifier_upgrade(claim) for claim in unit.claims):
            return True
    if verifier_insufficient and any(claim_needs_verifier_upgrade(claim) for claim in unit.claims):
        return True
    if any(item.kind in {"missing_step", "conflict"} for item in unit.uncertainty):
        return True
    if rapid_scene_changes(visual, unit.start_seconds, unit.end_seconds):
        if unit.kind == "procedure" or unit.evidence_requests:
            return True
    return False


def minimal_clip_range(unit: KnowledgeUnit, window: AnalysisWindow) -> tuple[float, float]:
    """Upload only the tightest range that still covers the unit (within clip limits)."""

    start = max(unit.start_seconds, window.core_start_seconds)
    end = min(unit.end_seconds, window.core_end_seconds)
    for request in unit.evidence_requests:
        start = max(start, request.start_seconds)
        end = min(end, request.end_seconds)
    for item in unit.uncertainty:
        if item.kind in {"missing_step", "conflict"}:
            start = max(start, item.start_seconds)
            end = min(end, item.end_seconds)
    length = end - start
    if length < MIN_CLIP_SECONDS:
        pad = (MIN_CLIP_SECONDS - length) / 2.0
        start = max(window.core_start_seconds, start - pad)
        end = min(window.core_end_seconds, end + pad)
    if end - start > MAX_CLIP_SECONDS:
        mid = (start + end) / 2.0
        start = mid - MAX_CLIP_SECONDS / 2.0
        end = mid + MAX_CLIP_SECONDS / 2.0
    start = max(window.core_start_seconds, start)
    end = min(window.core_end_seconds, end)
    if end - start < MIN_CLIP_SECONDS:
        end = min(window.core_end_seconds, start + MIN_CLIP_SECONDS)
    return start, end


def merge_native_units(
    frames_units: list[KnowledgeUnit],
    native_units: list[KnowledgeUnit],
) -> list[KnowledgeUnit]:
    """Merge native analysis into the frames path — one fact model, no duplicate units."""

    native_by_segment: dict[str, list[KnowledgeUnit]] = {}
    for unit in native_units:
        for segment_id in unit.segment_ids:
            native_by_segment.setdefault(segment_id, []).append(unit)

    merged: list[KnowledgeUnit] = []
    for unit in frames_units:
        candidates = native_by_segment.get(unit.segment_ids[0], [])
        if not candidates:
            merged.append(unit)
            continue
        native = candidates[0]
        claims_by_id = {claim.id: claim for claim in unit.claims}
        native_claim_ids = set(claims_by_id)
        for claim in native.claims:
            claims_by_id[claim.id] = claim
            native_claim_ids.add(claim.id)
        resolved_claims = [
            claim.model_copy(update={"status": "draft" if claim.status == "unresolved" else claim.status})
            for claim in claims_by_id.values()
        ]
        uncertainty = [item for item in unit.uncertainty if item.kind not in {"missing_step"}]
        evidence_requests = [] if native_claim_ids else list(unit.evidence_requests)
        merged.append(
            unit.model_copy(
                update={
                    "claims": resolved_claims,
                    "uncertainty": uncertainty,
                    "evidence_requests": evidence_requests,
                    "visual_candidates": unit.visual_candidates or native.visual_candidates,
                }
            )
        )
    return merged


def build_media_privacy_audit(
    *,
    source_id: str,
    analysis_mode: AnalysisMode,
    adapter: NativeVideoAdapter | None,
    budget: RefinementBudget | None = None,
) -> MediaPrivacyAudit:
    uploads = list(adapter.audit_records) if adapter is not None else []
    uploaded = any(record.state not in {"skipped", "planned"} for record in uploads)
    summary_parts = [
        f"analysis_mode={analysis_mode}",
        f"remote_uploads={len(uploads)}",
    ]
    if adapter is not None and adapter.probe is not None:
        summary_parts.append(f"retention={adapter.probe.remote_retention}")
    if budget is not None:
        summary_parts.append(f"frames_left={budget.remaining_frames}")
        summary_parts.append(f"clip_seconds_left={budget.remaining_clip_seconds:.1f}")
    return MediaPrivacyAudit(
        schema_version="1.0",
        source_id=source_id,
        analysis_mode=analysis_mode,
        media_uploaded=uploaded and analysis_mode != "frames",
        uploads=uploads,
        provider_probe=adapter.probe if adapter is not None else None,
        budget_frames_remaining=budget.remaining_frames if budget is not None else None,
        budget_clip_seconds_remaining=budget.remaining_clip_seconds if budget is not None else None,
        summary="; ".join(summary_parts),
    )


def apply_hybrid_native_pass(
    outcomes: list[SegmentAnalysisOutcome],
    *,
    analysis_mode: AnalysisMode,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap,
    capabilities: ProviderCapabilities,
    duration_seconds: float,
    native_adapter: NativeVideoAdapter | None,
    media_path: Path | None,
    budget: RefinementBudget | None,
    cancel_event: Event | None = None,
) -> tuple[list[SegmentAnalysisOutcome], RefinementBudget | None]:
    """Optionally upload native clips and merge results into existing outcomes."""

    from yt2class.adapters.providers.synthetic import synthetic_segment_from_payload
    from yt2class.stages.analyze_segments import (
        SegmentAnalysisOutcome,
        build_segment_payload,
        validate_knowledge_units,
    )
    from yt2class.stages.evidence_refinement import RefinementBudget, mark_unresolved
    from yt2class.stages.llm_util import payload_digest

    del capabilities, duration_seconds
    if analysis_mode == "frames" or native_adapter is None or not native_adapter.available:
        return outcomes, budget
    if media_path is None or not media_path.is_file():
        return outcomes, budget

    budget = budget or RefinementBudget()
    updated: list[SegmentAnalysisOutcome] = []

    def _native_units_for_window(
        *,
        window: AnalysisWindow,
        start: float,
        end: float,
        upload_id: str,
        clip_seconds: float,
    ) -> list[KnowledgeUnit]:
        clip = native_adapter.prepare_clip_upload(
            upload_id=upload_id,
            media_path=media_path,
            start_seconds=start,
            end_seconds=end,
            segment_id=window.id,
            duration_seconds=clip_seconds,
        )
        payload = build_segment_payload(
            window,
            transcript=transcript,
            visual=visual,
            course_map=course_map,
            analysis_mode=analysis_mode,
        )
        payload["native_clip"] = {
            "start_seconds": start,
            "end_seconds": end,
            "upload_id": upload_id,
        }
        prompt_digest = payload_digest(payload)
        analysis = native_adapter.upload_and_analyze(
            clip,
            prompt_digest=prompt_digest,
            cancel_event=cancel_event,
            delete_after=True,
        )
        budget.remaining_clip_seconds = max(0.0, budget.remaining_clip_seconds - clip_seconds)
        structured = analysis.structured
        if not structured.get("units"):
            structured = synthetic_segment_from_payload(
                {
                    **payload,
                    "segment_id": window.id,
                    "core_range": [start, end],
                }
            )
        return validate_knowledge_units(structured, payload)

    for outcome in outcomes:
        units = list(outcome.units)
        if analysis_mode == "hybrid" and not any(
            unit_needs_native_upgrade(unit, visual=visual) for unit in units
        ):
            updated.append(outcome)
            continue

        upgraded_units: list[KnowledgeUnit] = []

        if analysis_mode == "native-video":
            start = outcome.window.core_start_seconds
            end = outcome.window.core_end_seconds
            max_seconds = native_adapter.capabilities.max_video_seconds
            if end - start > max_seconds:
                end = start + max_seconds
            clip_seconds = end - start
            upload_id = f"native-{outcome.window.id}"
            try:
                native_units = _native_units_for_window(
                    window=outcome.window,
                    start=start,
                    end=end,
                    upload_id=upload_id,
                    clip_seconds=clip_seconds,
                )
                upgraded_units = native_units or units
            except NativeVideoError:
                upgraded_units = mark_unresolved(units, "native video analysis failed")
        else:
            for unit in units:
                if not unit_needs_native_upgrade(unit, visual=visual):
                    upgraded_units.append(unit)
                    continue
                start, end = minimal_clip_range(unit, outcome.window)
                clip_seconds = end - start
                if budget.remaining_clip_seconds < clip_seconds:
                    upgraded_units.extend(
                        mark_unresolved([unit], "native clip budget exhausted")
                    )
                    continue
                upload_id = f"native-{outcome.window.id}-{unit.id}"
                try:
                    native_units = _native_units_for_window(
                        window=outcome.window,
                        start=start,
                        end=end,
                        upload_id=upload_id,
                        clip_seconds=clip_seconds,
                    )
                    upgraded_units.append(merge_native_units([unit], native_units)[0])
                except NativeVideoError:
                    upgraded_units.extend(mark_unresolved([unit], "native video analysis failed"))

        merged = upgraded_units
        status = outcome.window.status
        if any(claim.status == "unresolved" for unit in merged for claim in unit.claims):
            status = "degraded"
        updated.append(
            SegmentAnalysisOutcome(
                window=outcome.window.model_copy(update={"status": status}),
                payload=outcome.payload,
                units=merged,
                repaired=outcome.repaired,
                error=outcome.error,
            )
        )
    return updated, budget
