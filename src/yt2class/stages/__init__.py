"""Pipeline stages; each stage consumes and emits versioned domain documents."""

from yt2class.stages.ingest import (
    IngestError,
    IngestResult,
    ingest_source,
    ingest_source_locked,
    resolve_manifest_media,
)
from yt2class.stages.extract_evidence import (
    EvidenceCancelled,
    extract_evidence,
    extract_evidence_locked,
)
from yt2class.stages.outline import outline_course, partition_transcript, reduce_outline
from yt2class.stages.analyze_segments import analyze_segments, analyze_window
from yt2class.stages.evidence_refinement import refine_window
from yt2class.stages.reduce_knowledge import reduce_knowledge
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.verify_claims import verify_claims
from yt2class.stages.review import apply_review_edits, build_review_bundle

__all__ = [
    "IngestError",
    "IngestResult",
    "ingest_source",
    "ingest_source_locked",
    "resolve_manifest_media",
    "extract_evidence",
    "extract_evidence_locked",
    "EvidenceCancelled",
    "outline_course",
    "partition_transcript",
    "reduce_outline",
    "analyze_segments",
    "analyze_window",
    "refine_window",
    "reduce_knowledge",
    "edit_deck",
    "verify_claims",
    "apply_review_edits",
    "build_review_bundle",
]
