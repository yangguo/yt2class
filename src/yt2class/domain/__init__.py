"""yt2class 3.0 domain documents. Additive; does not replace v2 SlideSpec."""

from yt2class.domain.course_map import CourseMap
from yt2class.domain.evidence import EvidenceArtifact, EvidenceBundle, EvidenceGap
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.migration import MigrationReport, assess_v2_migration, migrate_v2_to_v3
from yt2class.domain.render_report import RenderReport
from yt2class.domain.resolvers import ClosureError, DocumentBundle, resolve_reference_closure
from yt2class.domain.review import ReviewBundle, ReviewEdits, StaleReviewError
from yt2class.domain.run_manifest import RunManifest
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.domain.source import (
    SourceInput,
    SourceInputError,
    SourceManifest,
    content_sha256,
    read_source_inputs,
)
from yt2class.domain.transcript import (
    SpeechCoverage,
    TranscriptDocument,
    TranscriptGap,
    TranscriptSegment,
    TranscriptWord,
)
from yt2class.domain.timebase import TimeMapping, TimeMappingError
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import FrameOccurrence, FrameQuality, OcrRegion, VisualCatalogue, VisualGap

__all__ = [
    "ClosureError",
    "CourseMap",
    "EvidenceArtifact",
    "EvidenceBundle",
    "EvidenceGap",
    "DocumentBundle",
    "EditorialPlan",
    "KnowledgeDocument",
    "MigrationReport",
    "RenderReport",
    "ReviewBundle",
    "ReviewEdits",
    "StaleReviewError",
    "RunManifest",
    "SegmentManifest",
    "SourceInput",
    "SourceInputError",
    "SlideSpecV3",
    "SpeechCoverage",
    "SourceManifest",
    "TimeMapping",
    "TimeMappingError",
    "TranscriptDocument",
    "TranscriptGap",
    "TranscriptSegment",
    "TranscriptWord",
    "VerificationReport",
    "VisualCatalogue",
    "VisualGap",
    "FrameOccurrence",
    "FrameQuality",
    "OcrRegion",
    "assess_v2_migration",
    "migrate_v2_to_v3",
    "content_sha256",
    "read_source_inputs",
    "resolve_reference_closure",
]
