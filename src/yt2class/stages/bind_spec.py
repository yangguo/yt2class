"""Bind a reviewed EditorialPlan to SlideSpec 3.0 with filesystem-backed assets."""

from __future__ import annotations

from dataclasses import dataclass
import mimetypes
import re
from hashlib import sha256
from pathlib import Path
from typing import Iterable, Literal

from pydantic import ValidationError

from yt2class.domain.editorial import (
    EditorialPlan,
    PageIntent,
    PageQualityLabel,
    visible_quality_notes,
)
from yt2class.domain.knowledge import KnowledgeClaim, KnowledgeDocument
from yt2class.domain.slide_spec_v3 import (
    ClipEvidence,
    FrameEvidence,
    OcrEvidence,
    QuizQuestion,
    RenderPolicy,
    SequenceStep,
    SlideAsset,
    SlideClaim,
    SlidePage,
    SlideSource,
    SlideSpecV3,
    Theme,
    TranscriptEvidence,
)
from yt2class.domain.source import SourceManifest, content_sha256
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import QualityMode, VerificationReport, require_strict_closure
from yt2class.domain.visual import OcrRegion, VisualCatalogue, is_accepted_visual_occurrence
from yt2class.orchestration.workspace import Workspace, WorkspacePathError
from yt2class.stages.student_copy import contains_student_meta, sanitize_student_copy
from yt2class.stages.verify_claims import _is_practice

PRODUCER_VERSION = "yt2class-bind-1.0"
MAX_POINT_CLAIMS_PER_PAGE = 4
MAX_QUIZ_QUESTIONS_PER_PAGE = 3
MAX_SEQUENCE_STEPS = 3
DEFAULT_THEME = Theme(name="course-white-blue", font_family="Noto Sans CJK SC")
DEFAULT_RENDER_POLICY = RenderPolicy(
    max_pages=20,
    min_font_pt=18,
    aspect_ratio="16:9",
    overflow="repaginate-or-fail",
)


class BindError(ValueError):
    """Raised when editorial input cannot be bound to a trusted SlideSpec."""


@dataclass(frozen=True)
class BindResult:
    spec: SlideSpecV3
    spec_path: str


def file_digest(path: Path) -> str:
    return content_sha256(path)


def _resolved_under(root: Path, relative: str) -> Path:
    candidate = root / relative
    if candidate.is_symlink():
        raise BindError(f"symlink assets are not allowed: {relative}")
    resolved = candidate.resolve(strict=False)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise BindError(f"asset outside run root: {relative}")
    return resolved


def _reject_symlink(path: Path, root: Path) -> None:
    if path.is_symlink():
        raise BindError(f"symlink assets are not allowed: {path.relative_to(root)}")
    real = path.resolve(strict=False)
    if not real.is_relative_to(root.resolve(strict=True)):
        raise BindError(f"asset resolves outside run root: {path}")


def _mime_for_path(path: Path, declared: str) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if path.suffix.lower() in {".jpg", ".jpeg"} and not path.read_bytes()[:3] == b"\xff\xd8\xff":
        raise BindError(f"MIME mismatch for {path.name}: expected image/jpeg")
    if path.suffix.lower() == ".png" and not path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n":
        raise BindError(f"MIME mismatch for {path.name}: expected image/png")
    actual = guessed or declared
    if actual != declared and not (
        declared in {"image/jpeg", "image/jpg"} and actual in {"image/jpeg", "image/jpg", None}
    ):
        if actual and actual != declared:
            raise BindError(f"MIME mismatch for {path}: declared {declared}, detected {actual}")
    return declared


def validate_bound_assets(spec: SlideSpecV3, run_root: Path) -> None:
    """Check path containment, symlinks, hashes, and basic MIME for bound assets."""

    root = run_root.expanduser().resolve(strict=True)
    files: list[tuple[str, str, str | None]] = [
        (spec.source.media_path, spec.source.sha256, None),
    ]
    for asset in spec.assets:
        files.append((asset.path, asset.sha256, asset.mime_type))
    for relative, expected, mime in files:
        path = _resolved_under(root, relative)
        _reject_symlink(path, root)
        if not path.is_file():
            raise BindError(f"asset missing: {relative}")
        actual = file_digest(path)
        if actual != expected:
            raise BindError(f"asset hash mismatch: {relative}")
        if mime is not None:
            _mime_for_path(path, mime)


def _quality_status(
    report: VerificationReport,
    pages: Iterable[PageIntent],
) -> Literal["verified", "review_required", "incomplete", "evidence-only"]:
    if report.quality_mode == "evidence-only":
        return "evidence-only"
    labels = {page.quality_label for page in pages}
    if report.quality_mode == "strict" and labels <= {"verified"}:
        return "verified"
    if "evidence-only" in labels:
        return "evidence-only"
    if labels <= {"verified"}:
        return "verified"
    return "review_required"


def _claim_verdict(
    claim_id: str,
    report: VerificationReport,
    knowledge: KnowledgeDocument,
) -> Literal["supported", "contradicted", "insufficient"]:
    if report.quality_mode == "evidence-only":
        return "insufficient"
    by_id = {item.claim_id: item for item in report.verdicts}
    verdict = by_id.get(claim_id)
    if verdict is None:
        return "insufficient"
    return verdict.verdict


def _collect_claims(
    plan: EditorialPlan,
    knowledge: KnowledgeDocument,
    report: VerificationReport,
) -> dict[str, SlideClaim]:
    claims_by_id = {claim.id: claim for claim in knowledge.iter_claims()}
    referenced: set[str] = set()
    for page in plan.pages:
        referenced.update(page.claim_ids)
    built: dict[str, SlideClaim] = {}
    for claim_id in sorted(referenced):
        source_claim = claims_by_id.get(claim_id)
        if source_claim is None:
            raise BindError(f"unknown editorial claim {claim_id!r}")
        evidence_ids = _evidence_ids_for_knowledge_claim(source_claim)
        display = sanitize_student_copy(source_claim.text).strip()
        if not display:
            display = "…" if contains_student_meta(source_claim.text) else source_claim.text.strip()
        built[claim_id] = SlideClaim(
            id=claim_id,
            text=display,
            evidence_ids=evidence_ids,
            verdict=_claim_verdict(claim_id, report, knowledge),
            provenance=source_claim.provenance,
        )
    return built


def _evidence_ids_for_knowledge_claim(claim: KnowledgeClaim) -> list[str]:
    return [f"ev-{item}" for item in claim.evidence_ids]


def _retain_ocr_parent_frames(
    visual: VisualCatalogue,
    used_frames: set[str],
    *,
    evidence_refs: Iterable[str],
) -> None:
    """Ensure OCR-cited regions keep their parent frame in the asset closure."""

    occurrences = {item.id: item for item in visual.occurrences}
    regions = {region.id: region for region in visual.ocr_regions}
    for ref in evidence_refs:
        key = ref.removeprefix("ev-")
        region = regions.get(key)
        if region is None:
            continue
        parent_id = region.parent_occurrence_id
        if parent_id in occurrences:
            used_frames.add(parent_id)


def _frame_assets(
    visual: VisualCatalogue,
    frame_ids: Iterable[str],
) -> dict[str, SlideAsset]:
    assets = {asset.id: asset for asset in visual.assets}
    occurrences = {item.id: item for item in visual.occurrences}
    built: dict[str, SlideAsset] = {}
    for frame_id in frame_ids:
        occurrence = occurrences.get(frame_id)
        if occurrence is None or not is_accepted_visual_occurrence(occurrence, assets):
            raise BindError(f"unknown or rejected frame {frame_id!r}")
        visual_asset = assets[occurrence.asset_id]
        slide_asset_id = f"asset-{frame_id}"
        timestamp = occurrence.actual_source_seconds or occurrence.timestamp_seconds
        if timestamp is None:
            raise BindError(f"frame {frame_id!r} has no timestamp")
        built[slide_asset_id] = SlideAsset(
            id=slide_asset_id,
            role="frame",
            path=visual_asset.path,
            sha256=visual_asset.sha256,
            mime_type=visual_asset.mime_type,
            timestamp_seconds=timestamp,
        )
    return built


def _frame_evidence(frame_id: str, asset: SlideAsset) -> FrameEvidence:
    return FrameEvidence(
        id=f"ev-{frame_id}",
        kind="frame",
        asset_id=asset.id,
        timestamp_seconds=asset.timestamp_seconds or 0.0,
    )


def _transcript_assets(
    transcript: TranscriptDocument,
    claim: KnowledgeClaim,
    *,
    source_asset_path: str,
    source_sha256: str,
) -> tuple[list[SlideAsset], list[TranscriptEvidence]]:
    assets: list[SlideAsset] = []
    evidence: list[TranscriptEvidence] = []
    segments = {segment.id: segment for segment in transcript.segments}
    for ref in claim.evidence_ids:
        segment = segments.get(ref)
        if segment is None:
            continue
        asset_id = f"asset-transcript-{ref}"
        assets.append(
            SlideAsset(
                id=asset_id,
                role="transcript",
                path=source_asset_path,
                sha256=source_sha256,
                mime_type="application/json",
                start_seconds=segment.start_seconds,
                end_seconds=segment.end_seconds,
            )
        )
        evidence.append(
            TranscriptEvidence(
                id=f"ev-{ref}",
                kind="transcript",
                asset_id=asset_id,
                start_seconds=segment.start_seconds,
                end_seconds=segment.end_seconds,
                text=segment.text_original,
                origin=segment.origin,
            )
        )
    return assets, evidence


def _ocr_evidence(
    visual: VisualCatalogue,
    claim: KnowledgeClaim,
    frame_evidence: dict[str, FrameEvidence],
) -> list[OcrEvidence]:
    regions = {region.id: region for region in visual.ocr_regions}
    built: list[OcrEvidence] = []
    for ref in claim.evidence_ids:
        region = regions.get(ref)
        if region is None:
            continue
        parent = frame_evidence.get(f"ev-{region.parent_occurrence_id}")
        if parent is None:
            parent = frame_evidence.get(f"ev-{ref}")
        if parent is None:
            raise BindError(f"ocr {ref!r} missing parent frame evidence")
        _validate_ocr_bbox(region, visual)
        built.append(
            OcrEvidence(
                id=f"ev-{ref}",
                kind="ocr",
                parent_frame_evidence_id=parent.id,
                bbox=region.bbox,
                text=region.text,
                engine=region.engine,
            )
        )
    return built


def _validate_ocr_bbox(region: OcrRegion, visual: VisualCatalogue) -> None:
    assets = {asset.id: asset for asset in visual.assets}
    asset = assets.get(region.asset_id)
    if asset is None or asset.width is None or asset.height is None:
        return
    box = region.bbox
    if box.x + box.width > asset.width or box.y + box.height > asset.height:
        raise BindError(f"ocr bbox for {region.id} exceeds parent image bounds")


def _page_notes(page: PageIntent) -> str:
    cleaned = sanitize_student_copy(page.notes)
    if page.quality_label == "verified":
        return cleaned
    return visible_quality_notes(page.quality_label, cleaned)


def _content_bullets(page: PageIntent) -> list[str]:
    lines: list[str] = []
    for point in page.body_points:
        cleaned = sanitize_student_copy(point).strip()
        if cleaned:
            if len(cleaned) > 200:
                raise BindError(
                    f"learner bullet on page {page.id!r} exceeds the 200-character limit"
                )
            lines.append(cleaned)
    return lines[:4]


def _bind_cover(page: PageIntent, *, source_id: str, hero_asset_id: str | None) -> SlidePage:
    return SlidePage(
        id=page.id,
        type="cover",
        title=page.title,
        subtitle=page.body_points[0] if page.body_points else None,
        source_id=source_id,
        hero_asset_id=hero_asset_id,
        notes=_page_notes(page),
    )


def _frame_evidence_id(asset_id: str, evidence: dict[str, object]) -> str:
    for item in evidence.values():
        if isinstance(item, FrameEvidence) and item.asset_id == asset_id:
            return item.id
    raise BindError(f"visible frame asset {asset_id!r} has no frame evidence")


def _citations_for_page(
    page: PageIntent,
    *,
    slide_claims: dict[str, SlideClaim],
    evidence: dict[str, object],
    frame_assets: dict[str, SlideAsset],
) -> list[str]:
    cited = {
        ev
        for cid in page.claim_ids
        for ev in slide_claims[cid].evidence_ids
        if ev in evidence
    }
    if page.type == "content" and page.layout == "sequence":
        for frame_id in page.frame_ids[:MAX_SEQUENCE_STEPS]:
            asset = frame_assets.get(f"asset-{frame_id}")
            if asset is not None:
                cited.add(_frame_evidence_id(asset.id, evidence))
    elif page.type == "content" and page.layout in {"image-text", "comparison"}:
        for frame_id in page.frame_ids:
            asset = frame_assets.get(f"asset-{frame_id}")
            if asset is not None:
                cited.add(_frame_evidence_id(asset.id, evidence))
    return sorted(cited)


def _bind_content_pages(
    page: PageIntent,
    *,
    claims: dict[str, SlideClaim],
    frame_assets: dict[str, SlideAsset],
    evidence: dict[str, object],
) -> list[SlidePage]:
    layout = page.layout or "text"
    if layout == "sequence":
        if len(page.claim_ids) > MAX_SEQUENCE_STEPS:
            raise BindError("sequence layout supports at most 3 claims without continuation")
        if len(page.frame_ids) > MAX_SEQUENCE_STEPS:
            raise BindError("sequence layout supports at most 3 frames")
        steps: list[SequenceStep] = []
        for index, frame_id in enumerate(page.frame_ids):
            claim_id = page.claim_ids[index] if index < len(page.claim_ids) else page.claim_ids[0]
            asset = frame_assets.get(f"asset-{frame_id}")
            if asset is None:
                raise BindError(f"sequence step missing asset for {frame_id!r}")
            raw_caption = page.body_points[index] if index < len(page.body_points) else None
            caption = sanitize_student_copy(raw_caption)[:160] if raw_caption else None
            steps.append(
                SequenceStep(asset_id=asset.id, claim_ids=[claim_id], caption=caption)
            )
        if not 2 <= len(steps) <= MAX_SEQUENCE_STEPS:
            raise BindError("sequence layout requires 2-3 steps after binding")
        citations = _citations_for_page(
            page, slide_claims=claims, evidence=evidence, frame_assets=frame_assets
        )
        return [
            SlidePage(
                id=page.id,
                type="content",
                layout="sequence",
                title=page.title,
                steps=steps,
                citation_ids=citations,
                notes_claim_ids=page.claim_ids,
                notes=_page_notes(page),
            )
        ]

    if layout == "comparison":
        if len(page.frame_ids) != 2:
            raise BindError("comparison layout requires exactly 2 frames")

    def comparison_captions(chunk_claim_ids: list[str]) -> list[str]:
        if layout != "comparison":
            return []
        if len(page.body_points) >= 2:
            return [sanitize_student_copy(point)[:160] for point in page.body_points[:2]]
        if len(chunk_claim_ids) >= 2:
            return [claims[cid].text[:160] for cid in chunk_claim_ids[:2]]
        if chunk_claim_ids:
            text = claims[chunk_claim_ids[0]].text
            split = re.split(r"(?i)\s+(?:versus|vs\.?)\s+", text, maxsplit=1)
            if len(split) == 2:
                return [split[0][:160], split[1][:160]]
        raise BindError("comparison layout requires 2 captions in body_points or splittable claim text")

    point_chunks = [
        page.claim_ids[i : i + MAX_POINT_CLAIMS_PER_PAGE]
        for i in range(0, len(page.claim_ids), MAX_POINT_CLAIMS_PER_PAGE)
    ] or [[]]
    pages: list[SlidePage] = []
    for index, chunk in enumerate(point_chunks):
        slide_id = page.id if index == 0 else f"{page.id}-cont-{index + 1}"
        effective_layout = layout
        frame_ids = page.frame_ids
        captions = comparison_captions(chunk) if layout == "comparison" else []
        if index > 0:
            effective_layout = "text"
            frame_ids = []
            captions = []
        frame_asset_ids = [frame_assets[f"asset-{fid}"].id for fid in frame_ids]
        bullets = _content_bullets(page) if effective_layout in {"text", "image-text"} else []
        sub_page = page.model_copy(update={"claim_ids": chunk})
        citations = _citations_for_page(
            sub_page, slide_claims=claims, evidence=evidence, frame_assets=frame_assets
        )
        pages.append(
            SlidePage(
                id=slide_id,
                type="content",
                layout=effective_layout,
                title=page.title if index == 0 else f"{page.title} ({index + 1})",
                point_claim_ids=chunk,
                frame_asset_ids=frame_asset_ids,
                captions=captions,
                bullets=bullets,
                citation_ids=citations,
                notes_claim_ids=chunk,
                notes=_page_notes(page),
                continuation_of=page.id if index > 0 else None,
            )
        )
    return pages


def _summary_display_bullets(
    page: PageIntent,
    chunk: list[str],
    *,
    claims: dict[str, SlideClaim],
) -> list[str]:
    bullets: list[str] = []
    for index, claim_id in enumerate(chunk):
        if index < len(page.body_points) and page.body_points[index].strip():
            bullets.append(sanitize_student_copy(page.body_points[index])[:200])
        else:
            bullets.append(claims[claim_id].text)
    return bullets


def _bind_summary(page: PageIntent, *, claims: dict[str, SlideClaim]) -> list[SlidePage]:
    if not page.claim_ids:
        raise BindError(f"summary page {page.id!r} requires at least one claim")
    if page.body_points and len(page.body_points) > len(page.claim_ids):
        raise BindError(
            f"summary page {page.id!r} has {len(page.body_points)} learner bullets "
            f"and {len(page.claim_ids)} claim ids"
        )
    chunks = [page.claim_ids[i : i + 4] for i in range(0, len(page.claim_ids), 4)]
    pages: list[SlidePage] = []
    for index, chunk in enumerate(chunks):
        page_id = page.id if index == 0 else f"{page.id}-{index + 1}"
        offset = index * 4
        body_slice = page.body_points[offset : offset + len(chunk)]
        slice_page = page.model_copy(update={"body_points": body_slice})
        bullets = _summary_display_bullets(slice_page, chunk, claims=claims)
        pages.append(
            SlidePage(
                id=page_id,
                type="summary",
                title=page.title if index == 0 else f"{page.title} ({index + 1})",
                claim_ids=chunk,
                bullets=bullets,
                citation_ids=[ev for cid in chunk for ev in claims[cid].evidence_ids],
                notes=_page_notes(page),
                continuation_of=page.id if index > 0 else None,
            )
        )
    return pages


def _bind_quiz_pages(
    page: PageIntent,
    *,
    claims: dict[str, SlideClaim],
    evidence: dict[str, object],
    frame_assets: dict[str, SlideAsset],
) -> list[SlidePage]:
    chunks = [
        page.claim_ids[i : i + MAX_QUIZ_QUESTIONS_PER_PAGE]
        for i in range(0, len(page.claim_ids), MAX_QUIZ_QUESTIONS_PER_PAGE)
    ] or [[]]
    pages: list[SlidePage] = []
    for index, chunk in enumerate(chunks):
        questions: list[QuizQuestion] = []
        for claim_id in chunk:
            claim = claims[claim_id]
            questions.append(
                QuizQuestion(
                    prompt=claim.text[:240],
                    kind=(
                        "generated-practice"
                        if claim.provenance == "generated-practice"
                        else "source-question"
                    ),
                    answer_claim_ids=[claim_id],
                )
            )
        sub_page = page.model_copy(update={"claim_ids": chunk})
        citations = _citations_for_page(
            sub_page, slide_claims=claims, evidence=evidence, frame_assets=frame_assets
        )
        pages.append(
            SlidePage(
                id=page.id if index == 0 else f"{page.id}-cont-{index + 1}",
                type="quiz",
                title=page.title if index == 0 else f"{page.title} ({index + 1})",
                questions=questions,
                citation_ids=citations,
                notes_claim_ids=chunk,
                notes=_page_notes(page),
                continuation_of=page.id if index > 0 else None,
            )
        )
    return pages


def bind_editorial_plan(
    *,
    plan: EditorialPlan,
    knowledge: KnowledgeDocument,
    report: VerificationReport,
    source: SourceManifest,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    workspace: Workspace,
    transcript_path: str = "metadata/transcript.json",
    analysis_mode: Literal["frames", "native-video", "hybrid"] = "frames",
    theme: Theme | None = None,
    render_policy: RenderPolicy | None = None,
    spec_revision: int = 1,
) -> BindResult:
    """Produce SlideSpec 3.0 with closure over cited assets and evidence only."""

    if plan.source_id != source.source_id:
        raise BindError("editorial source_id does not match SourceManifest")
    if report.source_id != source.source_id:
        raise BindError("verification source_id does not match SourceManifest")
    if report.quality_mode == "strict":
        require_strict_closure(
            report,
            claim_ids=[claim.id for claim in knowledge.iter_claims()],
            label="SlideSpec binder",
        )

    slide_claims = _collect_claims(plan, knowledge, report)
    used_frames: set[str] = set()
    for page in plan.pages:
        used_frames.update(page.frame_ids)
    occurrences = {item.id: item for item in visual.occurrences}
    knowledge_by_id = {claim.id: claim for claim in knowledge.iter_claims()}
    for claim in slide_claims.values():
        for ref in claim.evidence_ids:
            key = ref.removeprefix("ev-")
            if key in occurrences:
                used_frames.add(key)
        _retain_ocr_parent_frames(visual, used_frames, evidence_refs=claim.evidence_ids)
        source_claim = knowledge_by_id.get(claim.id)
        if source_claim is not None:
            _retain_ocr_parent_frames(visual, used_frames, evidence_refs=source_claim.evidence_ids)

    frame_assets = _frame_assets(visual, used_frames)
    slide_assets: dict[str, SlideAsset] = dict(frame_assets)
    evidence: dict[str, FrameEvidence | TranscriptEvidence | OcrEvidence | ClipEvidence] = {}
    for frame_id, asset in frame_assets.items():
        fid = frame_id.removeprefix("asset-")
        item = _frame_evidence(fid, asset)
        evidence[item.id] = item

    media_rel = source.media_path
    media_path = workspace.safe_path(media_rel)
    if not media_path.is_file():
        raise BindError(f"source media missing: {media_rel}")
    actual_source_hash = file_digest(media_path)
    if actual_source_hash != source.sha256:
        raise BindError("source media hash does not match manifest")

    transcript_file = workspace.safe_path(transcript_path, create_parent=True)
    transcript_bytes = transcript.model_dump_json().encode("utf-8")
    transcript_file.write_bytes(transcript_bytes)
    transcript_digest = sha256(transcript_bytes).hexdigest()

    for claim in knowledge.iter_claims():
        if claim.id not in slide_claims:
            continue
        t_assets, t_evidence = _transcript_assets(
            transcript,
            claim,
            source_asset_path=transcript_path,
            source_sha256=transcript_digest,
        )
        for asset in t_assets:
            slide_assets[asset.id] = asset
        for item in t_evidence:
            evidence[item.id] = item
        for item in _ocr_evidence(visual, claim, {k: v for k, v in evidence.items() if isinstance(v, FrameEvidence)}):
            evidence[item.id] = item

    slide_source = SlideSource(
        source_id=source.source_id,
        kind=source.kind,
        title=source.title,
        media_path=media_rel,
        sha256=source.sha256,
        duration_seconds=source.duration_seconds,
        url=source.url,
    )

    bound_pages: list[SlidePage] = []
    for page in plan.pages:
        if page.type == "cover":
            hero = (
                frame_assets[f"asset-{page.frame_ids[0]}"].id if page.frame_ids else None
            )
            bound_pages.append(_bind_cover(page, source_id=source.source_id, hero_asset_id=hero))
        elif page.type == "summary":
            bound_pages.extend(_bind_summary(page, claims=slide_claims))
        elif page.type == "quiz" or _is_practice(knowledge, page.claim_ids):
            bound_pages.extend(
                _bind_quiz_pages(
                    page,
                    claims=slide_claims,
                    evidence=evidence,
                    frame_assets=frame_assets,
                )
            )
        else:
            bound_pages.extend(
                _bind_content_pages(
                    page,
                    claims=slide_claims,
                    frame_assets=frame_assets,
                    evidence=evidence,
                )
            )

    base_policy = render_policy or DEFAULT_RENDER_POLICY
    policy = base_policy.model_copy(update={"max_pages": min(base_policy.max_pages, plan.max_pages)})
    if len(bound_pages) > plan.max_pages:
        raise BindError("bound slides exceed editorial plan max_pages")
    if len(bound_pages) > policy.max_pages:
        raise BindError("bound slides exceed render_policy.max_pages")

    spec = SlideSpecV3(
        schema_version="3.0",
        spec_revision=spec_revision,
        producer_version=PRODUCER_VERSION,
        analysis_mode=analysis_mode,
        quality_status=_quality_status(report, plan.pages),
        source=slide_source,
        assets=sorted(slide_assets.values(), key=lambda item: item.id),
        evidence=sorted(evidence.values(), key=lambda item: item.id),
        claims=sorted(slide_claims.values(), key=lambda item: item.id),
        slides=bound_pages,
        theme=theme or DEFAULT_THEME,
        render_policy=policy,
    )
    validate_bound_assets(spec, workspace.root)
    rel_path = "delivery/slide-spec.v3.json"
    out = workspace.safe_path(rel_path, create_parent=True)
    out.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
    return BindResult(spec=spec, spec_path=rel_path)


__all__ = [
    "BindError",
    "BindResult",
    "bind_editorial_plan",
    "validate_bound_assets",
]
