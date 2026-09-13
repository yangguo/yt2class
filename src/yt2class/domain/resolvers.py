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
    if bundle.slide_spec.source.sha256 != bundle.source.sha256:
        raise ClosureError("SlideSpec source hash must match SourceManifest")

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

    spec_claims = {claim.id for claim in bundle.slide_spec.claims}
    for page in bundle.editorial.pages:
        unbound = set(page.claim_ids) - spec_claims
        if unbound:
            raise ClosureError(
                f"SlideSpec is missing editorial claim {sorted(unbound)}; "
                "binder must close claim references before render"
            )

    spec_frames = {
        asset.id for asset in bundle.slide_spec.assets if asset.role == "frame"
    } | {occurrence.id for occurrence in bundle.visual.occurrences}
    for page in bundle.editorial.pages:
        missing_frames = set(page.frame_ids) - spec_frames
        if missing_frames:
            raise ClosureError(
                f"SlideSpec/catalogue missing editorial frame {sorted(missing_frames)}"
            )
