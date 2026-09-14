"""Hybrid routing: upgrade to native video only when frames / verifier are insufficient."""

from __future__ import annotations

from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

from yt2class.adapters.providers.base import ProviderCapabilities, RequestCancelled
from yt2class.adapters.providers.native_video import (
    NativeVideoAdapter,
    NativeVideoError,
)
from yt2class.domain.course_map import CourseMap
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeUnit, Uncertainty
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.segment import AnalysisWindow
from yt2class.domain.slide_spec_v3 import AnalysisMode
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.native_clip import materialize_native_clip

if TYPE_CHECKING:
    from yt2class.stages.analyze_segments import SegmentAnalysisOutcome
    from yt2class.stages.evidence_refinement import ClipExtractor, RefinementBudget

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


def _clamp_interval(start: float, end: float, window: AnalysisWindow) -> tuple[float, float] | None:
    start = max(start, window.core_start_seconds)
    end = min(end, window.core_end_seconds)
    if end <= start:
        return None
    return start, end


def minimal_clip_range(unit: KnowledgeUnit, window: AnalysisWindow) -> tuple[float, float]:
    """Union span covering unit body and all evidence/uncertainty intervals (within the window)."""

    intervals: list[tuple[float, float]] = []
    base = _clamp_interval(unit.start_seconds, unit.end_seconds, window)
    if base is not None:
        intervals.append(base)
    for request in unit.evidence_requests:
        clipped = _clamp_interval(request.start_seconds, request.end_seconds, window)
        if clipped is not None:
            intervals.append(clipped)
    for item in unit.uncertainty:
        if item.kind in {"missing_step", "conflict"}:
            clipped = _clamp_interval(item.start_seconds, item.end_seconds, window)
            if clipped is not None:
                intervals.append(clipped)
    if not intervals:
        return window.core_start_seconds, min(
            window.core_end_seconds, window.core_start_seconds + MIN_CLIP_SECONDS
        )
    start = min(item[0] for item in intervals)
    end = max(item[1] for item in intervals)
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


def _dedupe_ids(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def merge_native_into_unit(
    frame_unit: KnowledgeUnit,
    native_units: list[KnowledgeUnit],
) -> KnowledgeUnit:
    """Attach native evidence to existing claims only; never invent parallel facts."""

    native_claims = [
        claim
        for native in native_units
        for claim in native.claims
        if native.end_seconds > frame_unit.start_seconds
        and native.start_seconds < frame_unit.end_seconds
    ]
    native_by_id = {claim.id: claim for claim in native_claims}
    updated_claims: list[KnowledgeClaim] = []
    for claim in frame_unit.claims:
        native = native_by_id.get(claim.id)
        if native is None:
            updated_claims.append(claim)
            continue
        merged_evidence = _dedupe_ids([*claim.evidence_ids, *native.evidence_ids])
        if claim.status == "unresolved":
            if native.status == "supported" and native.evidence_ids:
                updated_claims.append(
                    claim.model_copy(
                        update={
                            "evidence_ids": merged_evidence,
                            "status": "supported",
                        }
                    )
                )
            else:
                updated_claims.append(
                    claim.model_copy(update={"evidence_ids": merged_evidence})
                )
        elif native.status == "supported" and native.evidence_ids:
            updated_claims.append(
                claim.model_copy(
                    update={"evidence_ids": merged_evidence, "status": "supported"}
                )
            )
        else:
            updated_claims.append(
                claim.model_copy(update={"evidence_ids": merged_evidence})
            )

    uncertainty = list(frame_unit.uncertainty)
    evidence_requests = list(frame_unit.evidence_requests)
    resolved_missing = any(
        claim.id in native_by_id
        and native_by_id[claim.id].status == "supported"
        and native_by_id[claim.id].evidence_ids
        for claim in frame_unit.claims
    )
    if resolved_missing:
        uncertainty = [item for item in uncertainty if item.kind != "missing_step"]
        evidence_requests = [
            item for item in evidence_requests if "missing_step" not in item.reason
        ]

    return frame_unit.model_copy(
        update={
            "claims": updated_claims,
            "uncertainty": uncertainty,
            "evidence_requests": evidence_requests,
        }
    )


def mark_tail_unresolved(
    units: list[KnowledgeUnit],
    *,
    covered_end: float,
    note: str,
) -> list[KnowledgeUnit]:
    updated: list[KnowledgeUnit] = []
    for unit in units:
        if unit.start_seconds < covered_end - 1e-6:
            updated.append(unit)
            continue
        claims = [
            claim.model_copy(update={"status": "unresolved"})
            if claim.status in {"draft", "supported"}
            else claim
            for claim in unit.claims
        ]
        uncertainty = list(unit.uncertainty)
        uncertainty.append(
            Uncertainty(
                kind="missing_step",
                start_seconds=unit.start_seconds,
                end_seconds=unit.end_seconds,
                note=note[:400],
            )
        )
        updated.append(
            unit.model_copy(
                update={
                    "claims": claims,
                    "uncertainty": uncertainty,
                    "evidence_requests": [],
                }
            )
        )
    return updated


def build_media_privacy_audit(
    *,
    source_id: str,
    analysis_mode: AnalysisMode,
    adapter: NativeVideoAdapter | None,
    budget: RefinementBudget | None = None,
) -> MediaPrivacyAudit:
    uploads = list(adapter.audit_records) if adapter is not None else []
    media_uploaded = any(record.bytes_sent for record in uploads)
    billed_seconds = sum(record.usage_video_seconds for record in uploads if record.bytes_sent)
    summary_parts = [
        f"analysis_mode={analysis_mode}",
        f"remote_clips={len(uploads)}",
        f"bytes_sent={media_uploaded}",
        f"billed_video_seconds={billed_seconds:.1f}",
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
        media_uploaded=media_uploaded,
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
    output_dir: Path | None = None,
    clip_extractor: ClipExtractor | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[SegmentAnalysisOutcome], RefinementBudget | None, list[str]]:
    """Optionally upload native clips and merge results into existing outcomes."""

    from yt2class.stages.analyze_segments import (
        SegmentAnalysisOutcome,
        SegmentContractError,
        build_segment_payload,
        validate_knowledge_units,
    )
    from yt2class.stages.evidence_refinement import RefinementBudget, mark_unresolved
    from yt2class.stages.llm_util import payload_digest

    del duration_seconds
    gap_reasons: list[str] = []
    if analysis_mode == "frames" or native_adapter is None or not native_adapter.available:
        return outcomes, budget, gap_reasons
    if media_path is None or not media_path.is_file():
        return outcomes, budget, gap_reasons

    clip_root = output_dir or Path(".")
    budget = budget or RefinementBudget()
    updated: list[SegmentAnalysisOutcome] = []

    def _native_units_for_range(
        *,
        window: AnalysisWindow,
        start: float,
        end: float,
        upload_id: str,
    ) -> list[KnowledgeUnit]:
        clip_seconds = end - start
        clip_path = materialize_native_clip(
            media_path,
            start,
            end,
            clip_root,
            upload_id,
            clip_extractor=clip_extractor,
        )
        clip = native_adapter.prepare_clip_upload(
            upload_id=upload_id,
            clip_path=clip_path,
            start_seconds=start,
            end_seconds=end,
            segment_id=window.id,
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
            "clip_bytes": clip.byte_length,
        }
        prompt_digest = payload_digest(payload)
        try:
            analysis = native_adapter.upload_and_analyze(
                clip,
                prompt_digest=prompt_digest,
                cancel_event=cancel_event,
                delete_after=True,
            )
        except RequestCancelled:
            raise
        except NativeVideoError:
            raise
        try:
            return validate_knowledge_units(analysis.structured, payload)
        except SegmentContractError as error:
            raise NativeVideoError(str(error)) from error

    for outcome in outcomes:
        units = list(outcome.units)
        verifier_insufficient = any(
            claim.status == "insufficient" for unit in units for claim in unit.claims
        )
        if analysis_mode == "hybrid" and not any(
            unit_needs_native_upgrade(
                unit, visual=visual, verifier_insufficient=verifier_insufficient
            )
            for unit in units
        ):
            updated.append(outcome)
            continue

        merged_units: list[KnowledgeUnit] = list(units)
        window = outcome.window
        status = window.status
        window_error = outcome.error

        if analysis_mode == "native-video":
            core_start = window.core_start_seconds
            core_end = window.core_end_seconds
            max_seconds = native_adapter.capabilities.max_video_seconds
            covered_end = min(core_end, core_start + max_seconds)
            clip_seconds = covered_end - core_start
            partial_tail = covered_end < core_end - 1e-6
            if clip_seconds <= 0:
                merged_units = mark_unresolved(units, "native clip range empty")
                status = "degraded"
                gap_reasons.append(f"{window.id}: native clip range empty")
            elif budget.remaining_clip_seconds < clip_seconds:
                merged_units = mark_unresolved(units, "native clip budget exhausted")
                status = "degraded"
                gap_reasons.append(f"{window.id}: native clip budget exhausted")
            else:
                upload_id = f"native-{window.id}"
                try:
                    native_units = _native_units_for_range(
                        window=window,
                        start=core_start,
                        end=covered_end,
                        upload_id=upload_id,
                    )
                    budget.remaining_clip_seconds = max(
                        0.0, budget.remaining_clip_seconds - clip_seconds
                    )
                    merged_units = [
                        merge_native_into_unit(unit, native_units)
                        if unit.end_seconds <= covered_end + 1e-6
                        else unit
                        for unit in units
                    ]
                    if partial_tail:
                        merged_units = mark_tail_unresolved(
                            merged_units,
                            covered_end=covered_end,
                            note=f"native-video capped at {max_seconds:.0f}s; tail not analyzed",
                        )
                        status = "degraded"
                        gap_reasons.append(
                            f"{window.id}: native-video partial coverage [{core_start},{covered_end})"
                        )
                except (NativeVideoError, RequestCancelled) as error:
                    if isinstance(error, RequestCancelled):
                        raise
                    merged_units = mark_unresolved(units, "native video analysis failed")
                    status = "degraded"
                    window_error = str(error)[:400]
                    gap_reasons.append(f"{window.id}: native video failed; frames retained")
        else:
            hybrid_units: list[KnowledgeUnit] = []
            for unit in units:
                if not unit_needs_native_upgrade(
                    unit, visual=visual, verifier_insufficient=verifier_insufficient
                ):
                    hybrid_units.append(unit)
                    continue
                start, end = minimal_clip_range(unit, window)
                clip_seconds = end - start
                if budget.remaining_clip_seconds < clip_seconds:
                    hybrid_units.extend(
                        mark_unresolved([unit], "native clip budget exhausted")
                    )
                    status = "degraded"
                    continue
                upload_id = f"native-{window.id}-{unit.id}"
                try:
                    native_units = _native_units_for_range(
                        window=window,
                        start=start,
                        end=end,
                        upload_id=upload_id,
                    )
                    budget.remaining_clip_seconds = max(
                        0.0, budget.remaining_clip_seconds - clip_seconds
                    )
                    hybrid_units.append(merge_native_into_unit(unit, native_units))
                except (NativeVideoError, RequestCancelled) as error:
                    if isinstance(error, RequestCancelled):
                        raise
                    hybrid_units.extend(mark_unresolved([unit], "native video analysis failed"))
                    status = "degraded"
            merged_units = hybrid_units

        if any(claim.status == "unresolved" for unit in merged_units for claim in unit.claims):
            status = "degraded"
        updated.append(
            SegmentAnalysisOutcome(
                window=window.model_copy(update={"status": status}),
                payload=outcome.payload,
                units=merged_units,
                repaired=outcome.repaired,
                error=window_error,
            )
        )
    return updated, budget, gap_reasons
