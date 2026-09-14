"""Offline review bundle and the closed set of allowed editorial operations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from yt2class.adapters.providers.base import Provider
from yt2class.domain.course_map import CourseMap
from yt2class.domain.editorial import EditorialPlan, Omission, PageIntent, relabel_page
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.review import (
    ALLOWED_REVIEW_OPS,
    IllegalReviewOpError,
    ReviewBundle,
    ReviewEdits,
    ReviewOp,
    ReviewOpName,
    ReviewPageView,
    StaleReviewError,
)
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import (
    QualityMode,
    StrictClosureError,
    VerificationReport,
    require_strict_closure,
)
from yt2class.domain.visual import VisualCatalogue, is_accepted_visual_occurrence
from yt2class.stages.llm_util import payload_digest
from yt2class.stages.verify_claims import VerifyOutcome, page_copy_grounded, verify_claims

TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "templates" / "review.html"
SAFE_TIME_LINK = re.compile(r"^https://www\.youtube\.com/watch\?v=[A-Za-z0-9_-]{1,64}&t=\d+s$")
SAFE_LOCAL_LINK = re.compile(r"^t=\d+s$")
SAFE_ASSET_PATH = re.compile(
    r"^(?!/)(?!.*[:\\])(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+\.(?:jpg|jpeg|png|webp)$",
    re.I,
)
YOUTUBE_WATCH = re.compile(
    r"^https://(?:www\.)?youtube\.com/watch\?v=([A-Za-z0-9_-]{1,64})(?:&.*)?$"
)


def document_digest(model: Any) -> str:
    return payload_digest(model.model_dump(mode="json"))


def baseline_hashes(
    *,
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    report: VerificationReport | None = None,
) -> dict[str, str]:
    hashes = {
        "knowledge": document_digest(knowledge),
        "editorial": document_digest(plan),
        "transcript": document_digest(transcript),
        "visual": document_digest(visual),
    }
    if report is not None:
        hashes["verification"] = document_digest(report)
    return hashes


def accepted_frame_ids(visual: VisualCatalogue) -> set[str]:
    assets = {asset.id: asset for asset in visual.assets}
    return {
        occurrence.id
        for occurrence in visual.occurrences
        if is_accepted_visual_occurrence(occurrence, assets)
    }


def page_allowed_frames(
    page: PageIntent,
    *,
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue,
) -> set[str]:
    accepted = accepted_frame_ids(visual)
    relevant = {frame_id for frame_id in page.frame_ids if frame_id in accepted}
    claim_ids = set(page.claim_ids)
    for unit in knowledge.units:
        for claim in unit.claims:
            if claim.id not in claim_ids:
                continue
            cited = {item for item in claim.evidence_ids if item in accepted}
            relevant.update(cited)
            relevant.update(
                candidate.frame_id
                for candidate in unit.visual_candidates
                if candidate.frame_id in accepted and candidate.frame_id in cited
            )
    return relevant


def is_safe_time_link(value: str) -> bool:
    return bool(SAFE_TIME_LINK.fullmatch(value) or SAFE_LOCAL_LINK.fullmatch(value))


def is_safe_asset_path(value: str) -> bool:
    return bool(SAFE_ASSET_PATH.fullmatch(value))


def seek_link(seconds: float, source_url: str | None = None) -> str:
    stamp = max(0, int(seconds))
    local = f"t={stamp}s"
    if not source_url:
        return local
    match = YOUTUBE_WATCH.fullmatch(source_url)
    if match is None:
        return local
    return f"https://www.youtube.com/watch?v={match.group(1)}&t={stamp}s"


def _page_times(page: PageIntent, knowledge: KnowledgeDocument) -> list[float]:
    times: list[float] = []
    for unit in knowledge.units:
        if any(claim.id in page.claim_ids for claim in unit.claims):
            times.append(unit.start_seconds)
    return times


def _page_transcript(
    page: PageIntent,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
) -> list[str]:
    excerpts: list[str] = []
    claim_ids = set(page.claim_ids)
    evidence: list[str] = []
    for unit in knowledge.units:
        for claim in unit.claims:
            if claim.id in claim_ids:
                evidence.extend(claim.evidence_ids)
    by_id = {segment.id: segment.text_original for segment in transcript.segments}
    for item in evidence:
        if item in by_id:
            excerpts.append(by_id[item])
    if not excerpts:
        times = _page_times(page, knowledge)
        if times:
            start = min(times)
            for segment in transcript.segments:
                if segment.end_seconds > start and segment.start_seconds < start + 30:
                    excerpts.append(segment.text_original)
    return excerpts[:6]


def _claim_texts(page: PageIntent, knowledge: KnowledgeDocument) -> list[str]:
    by_id = {claim.id: claim.text for claim in knowledge.iter_claims()}
    return [by_id[item] for item in page.claim_ids if item in by_id]


def _frame_paths(page: PageIntent, visual: VisualCatalogue) -> list[str]:
    occ = {item.id: item for item in visual.occurrences}
    assets = {item.id: item for item in visual.assets}
    paths: list[str] = []
    for frame_id in page.frame_ids:
        occurrence = occ.get(frame_id)
        if occurrence is None:
            continue
        asset = assets.get(occurrence.asset_id)
        if asset is not None:
            if is_safe_asset_path(asset.path):
                paths.append(asset.path)
    return paths


def guard_strict_render(
    *,
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    report: VerificationReport,
) -> None:
    """A strict report, or any verified page, must close over the whole claim set.

    Without this a hand-written partial report — one supported verdict for a document
    with many claims — would still render pages labelled ``verified``.
    """

    verified = [page for page in plan.pages if page.quality_label == "verified"]
    if report.quality_mode != "strict" and not verified:
        return
    require_strict_closure(
        report,
        claim_ids={claim.id for claim in knowledge.iter_claims()},
        label="review render",
    )
    supported = {item.claim_id for item in report.verdicts if item.verdict == "supported"}
    unverified = sorted({claim_id for page in verified for claim_id in page.claim_ids} - supported)
    if unverified:
        raise StrictClosureError(f"verified review pages cite unverified claims {unverified}")


def build_review_bundle(
    *,
    knowledge: KnowledgeDocument,
    plan: EditorialPlan,
    report: VerificationReport,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    course_map: CourseMap | None = None,
    source_url: str | None = None,
    revision: int = 1,
) -> ReviewBundle:
    guard_strict_render(knowledge=knowledge, plan=plan, report=report)
    views: list[ReviewPageView] = []
    for page in plan.pages:
        times = _page_times(page, knowledge)
        views.append(
            ReviewPageView(
                page=page,
                claim_texts=_claim_texts(page, knowledge),
                transcript_excerpts=_page_transcript(page, knowledge, transcript),
                frame_ids=list(page.frame_ids),
                frame_asset_paths=_frame_paths(page, visual),
                time_links=[
                    link
                    for link in (seek_link(stamp, source_url) for stamp in times[:4])
                    if is_safe_time_link(link)
                ],
                selection_reason=page.selection_reason,
            )
        )
    omitted_topics: list[str] = []
    topic_titles = {topic.id: topic.title for topic in (course_map.topics if course_map else [])}
    for item in plan.omissions:
        if item.topic_id and item.topic_id in topic_titles:
            omitted_topics.append(f"{item.topic_id}: {topic_titles[item.topic_id]} — {item.reason}")
        elif item.claim_id:
            omitted_topics.append(f"{item.claim_id}: {item.reason}")
        else:
            omitted_topics.append(item.reason)
    return ReviewBundle(
        schema_version="1.0",
        source_id=plan.source_id,
        revision=revision,
        baseline_hashes=baseline_hashes(
            knowledge=knowledge, plan=plan, transcript=transcript, visual=visual, report=report
        ),
        plan=plan,
        report=report,
        pages=views,
        omitted_topics=list(dict.fromkeys(omitted_topics)),
        allowed_ops=list(ALLOWED_REVIEW_OPS),
        allowed_frame_ids=sorted(accepted_frame_ids(visual)),
    )


def render_review_html(bundle: ReviewBundle) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = bundle.model_dump_json()
    payload = payload.replace("<", "\\u003c")
    return template.replace("__REVIEW_BUNDLE_JSON__", payload)


def stub_binder(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {
        "status": "deferred",
        "milestone": "M4",
        "message": "SlideSpec 3.0 binder is not implemented in M3",
    }


def stub_renderer(*_args: Any, **_kwargs: Any) -> dict[str, str]:
    return {
        "status": "deferred",
        "milestone": "M4",
        "message": "PptxGenJS renderer is not implemented in M3",
    }


@dataclass
class ReviewApplyResult:
    bundle: ReviewBundle
    plan: EditorialPlan
    report: VerificationReport
    knowledge: KnowledgeDocument
    outcome: VerifyOutcome | None
    binder_status: dict[str, str]
    renderer_status: dict[str, str]


def _require_page(pages: list[PageIntent], page_id: str) -> PageIntent:
    for page in pages:
        if page.id == page_id:
            return page
    raise IllegalReviewOpError(f"unknown page {page_id}")


def apply_ops(
    plan: EditorialPlan,
    ops: Iterable[ReviewOp],
    *,
    knowledge: KnowledgeDocument,
    visual: VisualCatalogue,
) -> EditorialPlan:
    pages = list(plan.pages)
    omissions = list(plan.omissions)
    for op in ops:
        if op.op == "reorder":
            order = list(op.order or [])
            by_id = {page.id: page for page in pages}
            if set(order) != set(by_id):
                raise IllegalReviewOpError("reorder must list every remaining page exactly once")
            pages = [by_id[item] for item in order]
            continue
        page = _require_page(pages, op.page_id or "")
        if page.locked and op.op in {"edit_copy", "pick_asset", "delete"}:
            raise IllegalReviewOpError(f"page {page.id} is locked")
        if op.op == "lock":
            pages = [
                item.model_copy(update={"locked": True}) if item.id == page.id else item for item in pages
            ]
        elif op.op == "delete":
            omissions.append(Omission(claim_id=None, reason=f"reviewer deleted {page.id}"))
            for claim_id in page.claim_ids:
                omissions.append(Omission(claim_id=claim_id, reason=f"reviewer deleted {page.id}"))
            pages = [item for item in pages if item.id != page.id]
        elif op.op == "edit_copy":
            updates: dict[str, Any] = {}
            if op.title is not None:
                updates["title"] = op.title
            if op.notes is not None:
                updates["notes"] = op.notes
            if op.body_points is not None:
                updates["body_points"] = op.body_points
            pages = [item.model_copy(update=updates) if item.id == page.id else item for item in pages]
        elif op.op == "pick_asset":
            frames = list(op.frame_ids or [])
            allowed = page_allowed_frames(page, knowledge=knowledge, visual=visual)
            unknown = [item for item in frames if item not in allowed]
            if unknown:
                raise IllegalReviewOpError(
                    f"pick_asset cited rejected or unrelated frames {unknown}"
                )
            pages = [item.model_copy(update={"frame_ids": frames}) if item.id == page.id else item for item in pages]
        else:
            raise IllegalReviewOpError(f"unsupported review op {op.op}")
    if not pages:
        raise IllegalReviewOpError("review cannot delete the last page")
    return plan.model_copy(update={"pages": pages, "omissions": omissions})


def apply_review_edits(
    bundle: ReviewBundle,
    edits: ReviewEdits,
    *,
    knowledge: KnowledgeDocument,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    provider: Provider,
    course_map: CourseMap | None = None,
    source_url: str | None = None,
    allowed_ops: Iterable[ReviewOpName] | None = None,
    binder: Callable[..., dict[str, str]] | None = None,
    renderer: Callable[..., dict[str, str]] | None = None,
    quality_mode: QualityMode | None = None,
) -> ReviewApplyResult:
    permitted = tuple(allowed_ops) if allowed_ops is not None else tuple(bundle.allowed_ops)
    if edits.revision != bundle.revision:
        raise StaleReviewError("stale review revision")
    expected_keys = ["knowledge", "editorial", "transcript", "visual"]
    if "verification" in bundle.baseline_hashes:
        expected_keys.append("verification")
    expected = {key: bundle.baseline_hashes[key] for key in expected_keys}
    incoming = {key: edits.baseline_hashes.get(key) for key in expected}
    if incoming != expected:
        raise StaleReviewError("stale review baseline hash")
    for op in edits.ops:
        if op.op not in permitted:
            raise IllegalReviewOpError(f"op {op.op} is not allowed")
    planned = apply_ops(bundle.plan, edits.ops, knowledge=knowledge, visual=visual)
    affected: set[str] = set()
    force_draft: set[str] = set()
    original = {page.id: page for page in bundle.plan.pages}
    for op in edits.ops:
        if op.op in {"edit_copy", "pick_asset"} and op.page_id:
            page = next((item for item in planned.pages if item.id == op.page_id), original.get(op.page_id))
            if page is not None:
                affected.update(page.claim_ids)
            if op.op == "edit_copy" and page is not None:
                if not page_copy_grounded(
                    page, knowledge=knowledge, transcript=transcript, visual=visual, provider=provider
                ):
                    force_draft.add(page.id)
    outcome = None
    report = bundle.report
    current_knowledge = knowledge
    if affected:
        outcome = verify_claims(
            knowledge,
            plan=planned,
            transcript=transcript,
            visual=visual,
            provider=provider,
            quality_mode=quality_mode or bundle.report.quality_mode,
            existing=bundle.report,
            only_claim_ids=affected,
        )
        planned = outcome.plan
        report = outcome.report
        current_knowledge = outcome.knowledge
    if force_draft:
        planned = planned.model_copy(
            update={
                "pages": [
                    relabel_page(page, "draft") if page.id in force_draft else page
                    for page in planned.pages
                ]
            }
        )
    updated = build_review_bundle(
        knowledge=current_knowledge,
        plan=planned,
        report=report,
        transcript=transcript,
        visual=visual,
        course_map=course_map,
        source_url=source_url,
        revision=bundle.revision + 1,
    )
    if binder is not None:
        binder_status = binder(planned)
    else:
        binder_status = stub_binder(planned)
    if renderer is not None:
        renderer_status = renderer(planned)
    else:
        renderer_status = stub_renderer(planned)
    return ReviewApplyResult(
        bundle=updated,
        plan=planned,
        report=report,
        knowledge=current_knowledge,
        outcome=outcome,
        binder_status=binder_status,
        renderer_status=renderer_status,
    )
