"""M3 vertical path: EditorialPlan → VerificationReport → review bundle.

Independent of the prototype ``yt2class build`` pipeline and of M4 SlideSpec.
"""

from __future__ import annotations

from pathlib import Path

from yt2class.adapters.providers.base import Provider
from yt2class.adapters.providers.synthetic import fake_course_provider
from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import DeckOrder, EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.review import ReviewBundle
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import QualityMode, VerificationReport
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.analyze import default_capabilities
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages.review import build_review_bundle, render_review_html
from yt2class.stages.verify_claims import StrictVerificationError, VerifyOutcome, verify_claims


def plan_deck(
    knowledge: KnowledgeDocument,
    *,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap | None = None,
    provider: Provider | None = None,
    target_pages: int = 12,
    max_pages: int = 20,
    order: DeckOrder = "chronological",
) -> EditorialPlan:
    active = provider or fake_course_provider(default_capabilities())
    return edit_deck(
        knowledge,
        course_map=course_map,
        transcript=transcript,
        visual=visual,
        provider=active,
        target_pages=target_pages,
        max_pages=max_pages,
        order=order,
    )


def verify_plan(
    knowledge: KnowledgeDocument,
    *,
    plan: EditorialPlan,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider | None = None,
    quality_mode: QualityMode = "draft",
    existing: VerificationReport | None = None,
) -> VerifyOutcome:
    active = provider or fake_course_provider(default_capabilities())
    return verify_claims(
        knowledge,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=active,
        quality_mode=quality_mode,
        existing=existing,
    )


def write_editorial_artifacts(
    plan: EditorialPlan,
    output_dir: Path,
    *,
    report: VerificationReport | None = None,
    bundle: ReviewBundle | None = None,
    knowledge: KnowledgeDocument | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {"plan": output_dir / "editorial-plan.json"}
    paths["plan"].write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    if knowledge is not None:
        paths["knowledge"] = output_dir / "knowledge.json"
        paths["knowledge"].write_text(knowledge.model_dump_json(indent=2), encoding="utf-8")
    if report is not None:
        paths["report"] = output_dir / "verification-report.json"
        paths["report"].write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if bundle is not None:
        paths["review_json"] = output_dir / "review.json"
        paths["review_html"] = output_dir / "review.html"
        paths["review_json"].write_text(bundle.model_dump_json(indent=2), encoding="utf-8")
        paths["review_html"].write_text(render_review_html(bundle), encoding="utf-8")
    return paths


def load_persisted_knowledge(output_dir: Path, fallback: KnowledgeDocument) -> KnowledgeDocument:
    path = output_dir / "knowledge.json"
    if not path.exists():
        return fallback
    try:
        persisted = KnowledgeDocument.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback
    if persisted.source_id != fallback.source_id:
        return fallback
    return persisted


def load_persisted_report(output_dir: Path, source_id: str) -> VerificationReport | None:
    path = output_dir / "verification-report.json"
    if not path.exists():
        return None
    try:
        report = VerificationReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if report.source_id != source_id:
        return None
    return report


def load_persisted_revision(output_dir: Path, source_id: str) -> int:
    path = output_dir / "review.json"
    if not path.exists():
        return 1
    try:
        previous = ReviewBundle.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 1
    if previous.source_id != source_id:
        return 1
    return previous.revision


def build_review(
    *,
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    report: VerificationReport,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap | None = None,
    source_url: str | None = None,
    revision: int = 1,
    media_privacy: MediaPrivacyAudit | None = None,
) -> ReviewBundle:
    return build_review_bundle(
        knowledge=knowledge,
        plan=plan,
        report=report,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        source_url=source_url,
        revision=revision,
        media_privacy=media_privacy,
    )


__all__ = [
    "StrictVerificationError",
    "VerifyOutcome",
    "build_review",
    "load_persisted_knowledge",
    "load_persisted_report",
    "load_persisted_revision",
    "plan_deck",
    "verify_plan",
    "write_editorial_artifacts",
]
