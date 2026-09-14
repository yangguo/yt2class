"""M2 vertical path: outline → schedule → analyze → refine → reduce.

This entry is independent of the prototype ``yt2class build`` pipeline and
does not apply a final PPT page budget to analysis coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any

from yt2class.adapters.providers.base import Provider, ProviderCapabilities
from yt2class.adapters.providers.native_video import NativeVideoAdapter, fake_native_adapter
from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.slide_spec_v3 import AnalysisMode
from yt2class.domain.course_map import CourseMap
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.segment import SegmentManifest, cores_cover_duration
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.scheduler import SchedulerConfig, schedule_windows, set_window_status
from yt2class.stages.analyze_segments import SegmentAnalysisOutcome, analyze_segments
from yt2class.stages.evidence_refinement import RefinementBudget, refine_window
from yt2class.stages.outline import outline_course
from yt2class.stages.reduce_knowledge import reduce_knowledge


def default_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        supports_images=True,
        supports_video=False,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=8000,
        max_output_tokens=2000,
        max_images=8,
        max_video_seconds=0.0,
    )


@dataclass
class AnalysisResult:
    course_map: CourseMap
    segments: SegmentManifest
    knowledge: KnowledgeDocument
    visual: VisualCatalogue
    outcomes: list[SegmentAnalysisOutcome]
    coverage_complete: bool
    gap_reasons: list[str]
    media_audit: MediaPrivacyAudit | None = None

    def m2_gate_ok(self) -> bool:
        if not self.coverage_complete:
            return False
        claims = self.knowledge.iter_claims()
        if not claims:
            return False
        return all(claim.evidence_ids for claim in claims)


def analyze_course(
    *,
    source_id: str,
    duration_seconds: float,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
    capabilities: ProviderCapabilities | None = None,
    scheduler_config: SchedulerConfig | None = None,
    cancel_event: Event | None = None,
    page_budget: Any = None,
    output_dir: Path | None = None,
    frame_extractor: Any = None,
    clip_extractor: Any = None,
    analysis_mode: AnalysisMode = "frames",
    native_adapter: NativeVideoAdapter | None = None,
    media_path: Path | None = None,
) -> AnalysisResult:
    """Run the M2 understanding loop. ``page_budget`` is accepted and ignored."""

    del page_budget
    caps = capabilities or default_capabilities()
    active = provider or fake_course_provider(caps)
    course_map = outline_course(
        transcript,
        visual,
        active,
        source_id=source_id,
        duration_seconds=duration_seconds,
        cancel_event=cancel_event,
    )
    segments = schedule_windows(
        source_id=source_id,
        duration_seconds=duration_seconds,
        transcript=transcript,
        visual=visual,
        capabilities=caps,
        config=scheduler_config,
        cancel_event=cancel_event,
        course_map=course_map,
    )
    segments, units, outcomes = analyze_segments(
        segments,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        provider=active,
        cancel_event=cancel_event,
        analysis_mode=analysis_mode,
    )
    budget = RefinementBudget()
    current_visual = visual
    refined_outcomes: list[SegmentAnalysisOutcome] = []
    refined_units = []
    for outcome in outcomes:
        updated, current_visual, budget = refine_window(
            outcome,
            transcript=transcript,
            visual=current_visual,
            course_map=course_map,
            provider=active,
            capabilities=caps,
            duration_seconds=duration_seconds,
            output_dir=output_dir,
            frame_extractor=frame_extractor,
            clip_extractor=clip_extractor,
            budget=budget,
            cancel_event=cancel_event,
        )
        refined_outcomes.append(updated)
        refined_units.extend(updated.units)
        segments = segments.model_copy(
            update={
                "windows": [
                    updated.window if window.id == updated.window.id else window
                    for window in segments.windows
                ]
            }
        )
    if not refined_units:
        refined_units = units

    from yt2class.orchestration.hybrid_analysis import (
        apply_hybrid_native_pass,
        build_media_privacy_audit,
    )

    refined_outcomes, budget, hybrid_gaps = apply_hybrid_native_pass(
        refined_outcomes,
        analysis_mode=analysis_mode,
        transcript=transcript,
        visual=current_visual,
        course_map=course_map,
        capabilities=caps,
        duration_seconds=duration_seconds,
        native_adapter=native_adapter,
        media_path=media_path,
        budget=budget,
        output_dir=output_dir,
        clip_extractor=clip_extractor,
        cancel_event=cancel_event,
    )
    if refined_outcomes:
        refined_units = [unit for outcome in refined_outcomes for unit in outcome.units]
        for outcome in refined_outcomes:
            segments = set_window_status(
                segments,
                outcome.window.id,
                outcome.window.status,
                failure_reason=outcome.window.failure_reason,
            )

    knowledge = reduce_knowledge(refined_units, source_id=source_id, course_map=course_map)
    media_audit = build_media_privacy_audit(
        source_id=source_id,
        analysis_mode=analysis_mode,
        adapter=native_adapter,
        budget=budget,
    )
    gap_reasons = [
        window.failure_reason or window.status
        for window in segments.windows
        if window.status != "complete"
    ]
    gap_reasons.extend(hybrid_gaps)
    tiled = cores_cover_duration(segments.windows, duration_seconds)
    coverage = tiled and all(window.status == "complete" for window in segments.windows)
    if hybrid_gaps:
        coverage = False
    if not tiled:
        gap_reasons.append("core windows do not tile [0, duration)")
    unfinished = [window.id for window in segments.windows if window.status == "scheduled"]
    if unfinished:
        gap_reasons.append(f"unanalyzed windows: {unfinished}")
    return AnalysisResult(
        course_map=course_map,
        segments=segments,
        knowledge=knowledge,
        visual=current_visual,
        outcomes=refined_outcomes,
        coverage_complete=coverage,
        gap_reasons=gap_reasons,
        media_audit=media_audit,
    )


def resolve_native_adapter(
    *,
    analysis_mode: AnalysisMode,
    provider_name: str,
    max_video_seconds: float = 120.0,
) -> NativeVideoAdapter | None:
    """Attach fake native video transport when config opts into native/hybrid modes."""

    if analysis_mode == "frames":
        return None
    if provider_name not in {"fake", "fake-native"}:
        return None
    caps = default_capabilities().model_copy(
        update={"supports_video": True, "max_video_seconds": max_video_seconds}
    )
    return fake_native_adapter(capabilities=caps)


def analyze_evidence_bundle(
    bundle: EvidenceBundle,
    *,
    provider: Provider | None = None,
    capabilities: ProviderCapabilities | None = None,
    page_budget: Any = None,
    output_dir: Path | None = None,
    cancel_event: Event | None = None,
    frame_extractor: Any = None,
    clip_extractor: Any = None,
    analysis_mode: AnalysisMode = "frames",
    native_adapter: NativeVideoAdapter | None = None,
) -> AnalysisResult:
    media_path = None
    if bundle.source.media_path:
        candidate = Path(bundle.source.media_path)
        if candidate.is_file():
            media_path = candidate
    return analyze_course(
        source_id=bundle.source_id,
        duration_seconds=bundle.duration_seconds,
        transcript=bundle.transcript,
        visual=bundle.visual,
        provider=provider,
        capabilities=capabilities,
        page_budget=page_budget,
        output_dir=output_dir,
        cancel_event=cancel_event,
        frame_extractor=frame_extractor,
        clip_extractor=clip_extractor,
        analysis_mode=analysis_mode,
        native_adapter=native_adapter,
        media_path=media_path,
    )


def write_analysis_artifacts(result: AnalysisResult, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "course_map": output_dir / "course-map.json",
        "segments": output_dir / "segment-manifest.json",
        "knowledge": output_dir / "knowledge-document.json",
    }
    paths["course_map"].write_text(
        result.course_map.model_dump_json(indent=2), encoding="utf-8"
    )
    paths["segments"].write_text(result.segments.model_dump_json(indent=2), encoding="utf-8")
    paths["knowledge"].write_text(result.knowledge.model_dump_json(indent=2), encoding="utf-8")
    if result.media_audit is not None:
        audit_path = output_dir / "media-privacy-audit.json"
        audit_path.write_text(result.media_audit.model_dump_json(indent=2), encoding="utf-8")
        paths["media_audit"] = audit_path
    return paths
