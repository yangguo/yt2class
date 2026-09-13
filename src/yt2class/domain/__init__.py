"""yt2class 3.0 domain documents. Additive; does not replace v2 SlideSpec."""

from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.migration import MigrationReport, assess_v2_migration, migrate_v2_to_v3
from yt2class.domain.render_report import RenderReport
from yt2class.domain.resolvers import ClosureError, DocumentBundle, resolve_reference_closure
from yt2class.domain.run_manifest import RunManifest
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue

__all__ = [
    "ClosureError",
    "CourseMap",
    "DocumentBundle",
    "EditorialPlan",
    "KnowledgeDocument",
    "MigrationReport",
    "RenderReport",
    "RunManifest",
    "SegmentManifest",
    "SlideSpecV3",
    "SourceManifest",
    "TranscriptDocument",
    "VerificationReport",
    "VisualCatalogue",
    "assess_v2_migration",
    "migrate_v2_to_v3",
    "resolve_reference_closure",
]
