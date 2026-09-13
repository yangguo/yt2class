"""Published 3.0 document models and schema filenames."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel

from yt2class.domain.course_map import CourseMap
from yt2class.domain.evidence import EvidenceBundle
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.render_report import RenderReport
from yt2class.domain.run_manifest import RunManifest
from yt2class.domain.segment import SegmentManifest
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue

SCHEMA_MODELS: Mapping[str, type[BaseModel]] = {
    "source-manifest.v1.schema.json": SourceManifest,
    "evidence-bundle.v1.schema.json": EvidenceBundle,
    "transcript-document.v1.schema.json": TranscriptDocument,
    "visual-catalogue.v1.schema.json": VisualCatalogue,
    "segment-manifest.v1.schema.json": SegmentManifest,
    "course-map.v1.schema.json": CourseMap,
    "knowledge-document.v1.schema.json": KnowledgeDocument,
    "editorial-plan.v1.schema.json": EditorialPlan,
    "verification-report.v1.schema.json": VerificationReport,
    "slide-spec.v3.schema.json": SlideSpecV3,
    "run-manifest.v1.schema.json": RunManifest,
    "render-report.v1.schema.json": RenderReport,
}

SCHEMA_DESCRIPTIONS: Mapping[str, str] = {
    "source-manifest.v1.schema.json": (
        "SourceManifest 1.0 structural schema. Joins, half-open intervals, and "
        "filesystem binding are enforced in Python."
    ),
    "evidence-bundle.v1.schema.json": (
        "EvidenceBundle 1.0 structural schema. Transcript/visual joins and explicit "
        "coverage gaps are enforced in Python."
    ),
    "transcript-document.v1.schema.json": (
        "TranscriptDocument 1.0 structural schema. Unique segment IDs and half-open "
        "ranges are enforced in Python."
    ),
    "visual-catalogue.v1.schema.json": (
        "VisualCatalogue 1.0 structural schema. Occurrence/asset/OCR joins are "
        "enforced in Python."
    ),
    "segment-manifest.v1.schema.json": (
        "SegmentManifest 1.0 structural schema. Core/context interval pairing is "
        "enforced in Python."
    ),
    "course-map.v1.schema.json": (
        "CourseMap 1.0 structural schema. Topic relations and speculative-field "
        "policy are enforced in Python."
    ),
    "knowledge-document.v1.schema.json": (
        "KnowledgeDocument 1.0 structural schema. Claim/unit ID uniqueness and "
        "relation closure are enforced in Python."
    ),
    "editorial-plan.v1.schema.json": (
        "EditorialPlan 1.0 structural schema. Paths/hashes are forbidden; claim and "
        "frame joins are enforced in Python resolvers."
    ),
    "verification-report.v1.schema.json": (
        "VerificationReport 1.0 structural schema. Strict-mode unresolved claims "
        "are rejected in Python."
    ),
    "slide-spec.v3.schema.json": (
        "SlideSpec 3.0 structural schema. Layout unions, claim/evidence/asset "
        "closure, and verified-vs-evidence-only policy are enforced in Python. "
        "Path/hash filesystem binding is deferred to binder tests."
    ),
    "run-manifest.v1.schema.json": (
        "RunManifest 1.0 structural schema. Unique stage names are enforced in Python."
    ),
    "render-report.v1.schema.json": (
        "RenderReport 1.0 structural schema. Unique page mappings are enforced in Python."
    ),
}
