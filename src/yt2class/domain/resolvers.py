"""Cross-document reference closure for the 3.0 evidence chain.

JSON Schema cannot join documents. These resolvers check that source, asset,
evidence, claim, and page IDs form a closed graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue


class ClosureError(ValueError):
    """Raised when a cross-document reference cannot be resolved."""


@dataclass(frozen=True)
class DocumentBundle:
    source: SourceManifest
    transcript: TranscriptDocument
    visual: VisualCatalogue
    segments: SegmentManifest
    course_map: CourseMap
    knowledge: KnowledgeDocument
    editorial: EditorialPlan
    verification: VerificationReport
    slide_spec: SlideSpecV3


def _same_source(*source_ids: str) -> None:
    if len(set(source_ids)) != 1:
        raise ClosureError(
            f"source_id mismatch across documents: {sorted(set(source_ids))}; "
            "JSON Schema cannot check cross-document identity"
        )


def evidence_universe(transcript: TranscriptDocument, visual: VisualCatalogue) -> set[str]:
    """IDs that knowledge/editorial may cite before SlideSpec binding."""

    ids = {segment.id for segment in transcript.segments}
    ids.update(occurrence.id for occurrence in visual.occurrences)
    ids.update(region.id for region in visual.ocr_regions)
    ids.update(asset.id for asset in visual.assets)
    return ids


def _check_point(value: float, duration: float, *, label: str) -> None:
    if not 0 <= value < duration:
        raise ClosureError(f"{label} {value} is outside source duration {duration}")


def _check_range(start: float, end: float, duration: float, *, label: str) -> None:
    if not 0 <= start < end <= duration:
        raise ClosureError(
            f"{label} [{start}, {end}) exceeds source duration {duration}"
        )


def _check_timeline_bounds(bundle: DocumentBundle) -> None:
    """Enforce one source timebase across all cross-document intervals."""

    duration = bundle.source.duration_seconds
    coverage = bundle.transcript.speech_coverage
    if bundle.transcript.duration_seconds is not None and bundle.transcript.duration_seconds > duration:
        raise ClosureError("transcript document duration exceeds source duration")
    if coverage.denominator_seconds is not None and coverage.denominator_seconds > duration:
        raise ClosureError("transcript coverage denominator exceeds source duration")
    if (
        coverage.denominator == "timeline"
        and coverage.denominator_seconds is not None
        and coverage.denominator_seconds != duration
    ):
        raise ClosureError("timeline transcript coverage denominator must match source duration")
    if coverage.speech_seconds > duration:
        raise ClosureError("transcript speech coverage exceeds source duration")
    if coverage.covered_seconds > duration:
        raise ClosureError("transcript covered duration exceeds source duration")
    if coverage.covered_seconds > coverage.speech_seconds:
        raise ClosureError("transcript covered duration exceeds speech duration")

    for segment in bundle.transcript.segments:
        _check_range(
            segment.start_seconds,
            segment.end_seconds,
            duration,
            label=f"transcript segment {segment.id}",
        )
        for word in segment.words or []:
            _check_range(
                word.start_seconds,
                word.end_seconds,
                duration,
                label=f"transcript word in {segment.id}",
            )
            if word.start_seconds < segment.start_seconds or word.end_seconds > segment.end_seconds:
                raise ClosureError(
                    f"transcript word in {segment.id} falls outside its segment"
                )
    for gap in bundle.transcript.gaps:
        _check_range(
            gap.start_seconds,
            gap.end_seconds,
            duration,
            label=f"transcript gap {gap.id}",
        )

    scenes = {scene.id: scene for scene in bundle.visual.scenes}
    for scene in bundle.visual.scenes:
        _check_range(
            scene.start_seconds,
            scene.end_seconds,
            duration,
            label=f"scene {scene.id}",
        )
    for occurrence in bundle.visual.occurrences:
        _check_point(
            occurrence.requested_seconds,
            duration,
            label=f"occurrence {occurrence.id} requested timestamp",
        )
        if occurrence.timestamp_seconds is not None:
            _check_point(
                occurrence.timestamp_seconds,
                duration,
                label=f"occurrence {occurrence.id} timestamp",
            )
        if occurrence.actual_source_seconds is not None:
            _check_point(
                occurrence.actual_source_seconds,
                duration,
                label=f"occurrence {occurrence.id} actual timestamp",
            )
        scene = scenes.get(occurrence.scene_id)
        if scene is None:
            raise ClosureError(
                f"occurrence {occurrence.id} cites unknown scene {occurrence.scene_id}"
            )
        if (
            occurrence.timestamp_seconds is not None
            and not scene.start_seconds <= occurrence.timestamp_seconds < scene.end_seconds
        ):
            raise ClosureError(
                f"occurrence {occurrence.id} timestamp is outside scene {scene.id}"
            )
        if (
            occurrence.actual_source_seconds is not None
            and not scene.start_seconds <= occurrence.actual_source_seconds < scene.end_seconds
        ):
            raise ClosureError(
                f"occurrence {occurrence.id} actual timestamp is outside scene {scene.id}"
            )
    for gap in bundle.visual.gaps:
        _check_range(gap.start_seconds, gap.end_seconds, duration, label=f"visual gap {gap.id}")

    for topic in bundle.course_map.topics:
        _check_range(
            topic.start_seconds,
            topic.end_seconds,
            duration,
            label=f"course topic {topic.id}",
        )

    for window in bundle.segments.windows:
        _check_range(
            window.core_start_seconds,
            window.core_end_seconds,
            duration,
            label=f"analysis window {window.id} core",
        )
        _check_range(
            window.context_start_seconds,
            window.context_end_seconds,
            duration,
            label=f"analysis window {window.id} context",
        )

    for unit in bundle.knowledge.units:
        _check_range(
            unit.start_seconds,
            unit.end_seconds,
            duration,
            label=f"knowledge unit {unit.id}",
        )
        for uncertainty in unit.uncertainty:
            _check_range(
                uncertainty.start_seconds,
                uncertainty.end_seconds,
                duration,
                label=f"uncertainty in {unit.id}",
            )
        for request in unit.evidence_requests:
            _check_range(
                request.start_seconds,
                request.end_seconds,
                duration,
                label=f"evidence request in {unit.id}",
            )


def _expected_verdict(status: str) -> str:
    """Map intermediate knowledge statuses to the final verifier vocabulary."""

    if status in {"draft", "unresolved"}:
        return "insufficient"
    return status


def resolve_reference_closure(bundle: DocumentBundle) -> None:
    """Reject any dangling source, evidence, claim, asset, or page reference."""

    _same_source(
        bundle.source.source_id,
        bundle.transcript.source_id,
        bundle.visual.source_id,
        bundle.segments.source_id,
        bundle.course_map.source_id,
        bundle.knowledge.source_id,
        bundle.editorial.source_id,
        bundle.verification.source_id,
        bundle.slide_spec.source.source_id,
    )
    if bundle.segments.duration_seconds != bundle.source.duration_seconds:
        raise ClosureError("SegmentManifest duration must match SourceManifest")
    if bundle.slide_spec.source.duration_seconds != bundle.source.duration_seconds:
        raise ClosureError("SlideSpec duration must match SourceManifest")
    if bundle.slide_spec.source.sha256 != bundle.source.sha256:
        raise ClosureError("SlideSpec source hash must match SourceManifest")

    _check_timeline_bounds(bundle)

    evidence_ids = evidence_universe(bundle.transcript, bundle.visual)
    topics = {topic.id for topic in bundle.course_map.topics}
    windows = {window.id for window in bundle.segments.windows}
    frame_ids = {occurrence.id for occurrence in bundle.visual.occurrences}
    claims = {claim.id: claim for claim in bundle.knowledge.iter_claims()}
    units = {unit.id for unit in bundle.knowledge.units}

    for topic in bundle.course_map.topics:
        missing = set(topic.evidence_ids) - evidence_ids
        if missing:
            raise ClosureError(f"CourseMap topic {topic.id} cites unknown evidence {sorted(missing)}")

    for window in bundle.segments.windows:
        missing = set(window.evidence_ids) - evidence_ids
        if missing:
            raise ClosureError(f"window {window.id} cites unknown evidence {sorted(missing)}")

    for unit in bundle.knowledge.units:
        if unit.topic_id not in topics:
            raise ClosureError(f"knowledge unit {unit.id} cites unknown topic {unit.topic_id}")
        missing_segments = set(unit.segment_ids) - windows
        if missing_segments:
            raise ClosureError(
                f"knowledge unit {unit.id} cites unknown segment {sorted(missing_segments)}"
            )
        for claim in unit.claims:
            # Knowledge cites catalogue IDs, not necessarily SlideSpec evidence IDs.
            missing = set(claim.evidence_ids) - evidence_ids
            if missing:
                raise ClosureError(f"claim {claim.id} cites unknown evidence {sorted(missing)}")
        for candidate in unit.visual_candidates:
            if candidate.frame_id not in frame_ids:
                raise ClosureError(
                    f"unit {unit.id} visual candidate cites unknown frame {candidate.frame_id}"
                )

    for page in bundle.editorial.pages:
        missing_claims = set(page.claim_ids) - set(claims)
        if missing_claims:
            raise ClosureError(f"editorial page {page.id} cites unknown claim {sorted(missing_claims)}")
        missing_frames = set(page.frame_ids) - frame_ids
        if missing_frames:
            raise ClosureError(f"editorial page {page.id} cites unknown frame {sorted(missing_frames)}")
        if page.type == "cover" and page.claim_ids:
            # Cover may omit claims; extra claims are allowed only if they exist (already checked).
            pass

    for omission in bundle.editorial.omissions:
        if omission.topic_id is not None and omission.topic_id not in topics:
            raise ClosureError(f"omission cites unknown topic {omission.topic_id}")
        if omission.claim_id is not None and omission.claim_id not in claims:
            raise ClosureError(f"omission cites unknown claim {omission.claim_id}")

    verdict_ids = {item.claim_id for item in bundle.verification.verdicts}
    if set(claims) - verdict_ids:
        raise ClosureError(
            f"verification missing claims {sorted(set(claims) - verdict_ids)}"
        )
    extra = verdict_ids - set(claims)
    if extra:
        raise ClosureError(f"verification cites unknown claims {sorted(extra)}")
    pending = set(bundle.verification.pending_review) - set(claims) - units
    if pending:
        raise ClosureError(f"verification pending_review cites unknown ids {sorted(pending)}")
    extra_removed = set(bundle.verification.removed_from_formal) - set(claims)
    if extra_removed:
        raise ClosureError(
            f"verification removed_from_formal cites unknown claims {sorted(extra_removed)}"
        )
    extra_repaired = set(bundle.verification.repaired_claim_ids) - set(claims)
    if extra_repaired:
        raise ClosureError(
            f"verification repaired_claim_ids cites unknown claims {sorted(extra_repaired)}"
        )

    verification_by_claim = {
        verdict.claim_id: verdict for verdict in bundle.verification.verdicts
    }
    for verdict in bundle.verification.verdicts:
        supporting = set(verdict.supporting_ids)
        contradicting = set(verdict.contradicting_ids)
        overlap = supporting & contradicting
        if overlap:
            raise ClosureError(
                f"verification claim {verdict.claim_id} cites evidence as both "
                f"supporting and contradicting: {sorted(overlap)}"
            )
        missing_evidence = (supporting | contradicting) - evidence_ids
        if missing_evidence:
            raise ClosureError(
                f"verification claim {verdict.claim_id} cites unknown evidence "
                f"{sorted(missing_evidence)}"
            )
        if verdict.verdict == "supported" and not supporting:
            raise ClosureError(
                f"supported verification claim {verdict.claim_id} requires supporting evidence"
            )
        if verdict.verdict == "contradicted" and not contradicting:
            raise ClosureError(
                f"contradicted verification claim {verdict.claim_id} requires contradicting evidence"
            )

    spec_claims = {claim.id: claim for claim in bundle.slide_spec.claims}
    # The binder may retain additional selected claims for notes or later
    # repagination, but every SlideSpec claim must originate in Knowledge.
    extra_spec_claims = set(spec_claims) - set(claims)
    if extra_spec_claims:
        raise ClosureError(f"SlideSpec cites unknown knowledge claims {sorted(extra_spec_claims)}")
    quality_pairs = {
        "strict": {"verified"},
        "draft": {"review_required", "incomplete"},
        "evidence-only": {"evidence-only"},
    }
    allowed_statuses = quality_pairs[bundle.verification.quality_mode]
    if bundle.slide_spec.quality_status not in allowed_statuses:
        raise ClosureError(
            f"verification quality_mode {bundle.verification.quality_mode!r} is incompatible "
            f"with SlideSpec quality_status {bundle.slide_spec.quality_status!r}"
        )
    if bundle.verification.quality_mode == "evidence-only" and any(
        verdict.verdict == "supported" for verdict in bundle.verification.verdicts
    ):
        raise ClosureError("evidence-only verification cannot contain supported claims")

    for claim_id, claim in claims.items():
        verification = verification_by_claim[claim_id]
        expected = _expected_verdict(claim.status)
        if verification.verdict != expected:
            raise ClosureError(
                f"verification verdict for claim {claim_id} ({verification.verdict}) "
                f"does not match Knowledge status {claim.status}"
            )
    for claim_id, slide_claim in spec_claims.items():
        verification = verification_by_claim[claim_id]
        if slide_claim.verdict != verification.verdict:
            raise ClosureError(
                f"SlideSpec verdict for claim {claim_id} ({slide_claim.verdict}) "
                f"does not match verification verdict {verification.verdict}"
            )
    for page in bundle.editorial.pages:
        unbound = set(page.claim_ids) - set(spec_claims)
        if unbound:
            raise ClosureError(
                f"SlideSpec is missing editorial claim {sorted(unbound)}; "
                "binder must close claim references before render"
            )

    occurrences = {occurrence.id: occurrence for occurrence in bundle.visual.occurrences}
    spec_assets = {asset.id: asset for asset in bundle.slide_spec.assets}
    for page in bundle.editorial.pages:
        for frame_id in page.frame_ids:
            occurrence = occurrences[frame_id]
            bound = spec_assets.get(occurrence.asset_id)
            if bound is None or bound.role != "frame":
                raise ClosureError(
                    f"SlideSpec is missing bound frame asset {occurrence.asset_id!r} "
                    f"for editorial frame {frame_id!r}; occurrence ids are not a substitute"
                )
