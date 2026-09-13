"""Explicit v2 → 3.0 migration. Missing claim refs stay unresolved/evidence-only."""

from __future__ import annotations

from typing import Any, Literal

from yt2class.domain.common import StrictModel
from yt2class.domain.slide_spec_v3 import (
    FrameEvidence,
    SlideAsset,
    SlideClaim,
    SlidePage,
    SlideSource,
    SlideSpecV3,
    Theme,
    TranscriptEvidence,
    RenderPolicy,
)
from yt2class.slide_spec import SlideSpec as SlideSpecV2

AllowedMigrationStatus = Literal["evidence-only", "incomplete"]


class MigrationIssue(StrictModel):
    kind: Literal["missing_claim_ref", "unsupported_upgrade", "unresolved"]
    location: str
    message: str


class MigrationReport(StrictModel):
    source_schema: Literal["2.0"]
    target_schema: Literal["3.0"]
    allowed_quality_status: AllowedMigrationStatus
    can_mark_supported: Literal[False] = False
    issues: list[MigrationIssue]
    notes: str


def assess_v2_migration(payload: SlideSpecV2 | dict[str, Any]) -> MigrationReport:
    """Describe why v2 text cannot become a verified 3.0 SlideSpec."""

    spec = payload if isinstance(payload, SlideSpecV2) else SlideSpecV2.model_validate(payload)
    issues: list[MigrationIssue] = [
        MigrationIssue(
            kind="unsupported_upgrade",
            location="schema_version",
            message=(
                "v2 has no claim objects; unknown schema major must not auto-upgrade "
                "to supported. Formal 3.0 claims require new evidence binding and verification."
            ),
        )
    ]
    locations = [f"slides[{index}].points[{p}]" for index, slide in enumerate(spec.slides) for p in range(len(slide.points))]
    locations.extend(f"summary[{index}]" for index in range(len(spec.summary)))
    locations.extend(f"quiz[{index}].answer" for index in range(len(spec.quiz)))
    for location in locations:
        issues.append(
            MigrationIssue(
                kind="missing_claim_ref",
                location=location,
                message=(
                    "v2 grounded text cites evidence_ids only and has no claim_id; "
                    "it can be unresolved or evidence-only, never supported"
                ),
            )
        )
    return MigrationReport(
        source_schema="2.0",
        target_schema="3.0",
        allowed_quality_status="evidence-only",
        can_mark_supported=False,
        issues=issues,
        notes=(
            "Old lesson text without claim references cannot be migrated to a "
            "verified SlideSpec. Re-run editor+verifier in M3, or keep evidence-only."
        ),
    )


def migrate_v2_to_v3(
    payload: SlideSpecV2 | dict[str, Any],
    *,
    quality_status: AllowedMigrationStatus = "evidence-only",
) -> tuple[MigrationReport, SlideSpecV3]:
    """Emit an evidence-only 3.0 spec. Refuses verified/supported upgrades."""

    if quality_status not in {"evidence-only", "incomplete"}:
        raise ValueError("v2 migration may only target evidence-only or incomplete")
    spec = payload if isinstance(payload, SlideSpecV2) else SlideSpecV2.model_validate(payload)
    report = assess_v2_migration(spec)
    assets = [
        SlideAsset(
            id=asset.id,
            role="frame",
            path=asset.path,
            sha256=asset.sha256,
            mime_type="image/jpeg",
            timestamp_seconds=asset.timestamp_seconds,
        )
        for asset in spec.assets
    ]
    evidence: list[FrameEvidence | TranscriptEvidence] = []
    if any(item.kind == "transcript" for item in spec.evidence):
        assets.append(
            SlideAsset(
                id="asset-transcript-migrated",
                role="transcript",
                path="evidence/transcript.json",
                sha256=spec.source.sha256,
                mime_type="application/json",
            )
        )
    for item in spec.evidence:
        if item.kind == "frame":
            evidence.append(
                FrameEvidence(
                    id=item.id,
                    kind="frame",
                    asset_id=item.asset_id,
                    timestamp_seconds=next(
                        asset.timestamp_seconds for asset in spec.assets if asset.id == item.asset_id
                    ),
                )
            )
        else:
            evidence.append(
                TranscriptEvidence(
                    id=item.id,
                    kind="transcript",
                    asset_id="asset-transcript-migrated",
                    start_seconds=item.start_seconds,
                    end_seconds=item.end_seconds,
                    text=item.text or "migrated transcript",
                    origin=item.origin or "manual-caption",
                )
            )
    claims: list[SlideClaim] = []
    slides: list[SlidePage] = []
    # Cover is required in 3.0 physical page lists.
    slides.append(
        SlidePage(
            id="page-cover",
            type="cover",
            title=spec.title,
            source_id="src-migrated",
            citation_ids=[],
            notes_claim_ids=[],
            notes="[evidence-only] migrated from SlideSpec v2; claims are unresolved",
        )
    )
    for index, slide in enumerate(spec.slides, start=1):
        claim_id = f"claim-migrated-{index}"
        point_ids = [f"{claim_id}-p{p}" for p in range(len(slide.points))]
        for point_index, point in enumerate(slide.points):
            claims.append(
                SlideClaim(
                    id=point_ids[point_index],
                    text=point.text,
                    evidence_ids=list(point.evidence_ids),
                    verdict="insufficient",
                    provenance="source",
                )
            )
        frame_evidence = next(
            item.id for item in spec.evidence if item.kind == "frame" and item.asset_id == slide.asset_id
        )
        slides.append(
            SlidePage(
                id=slide.id,
                type="content",
                layout="image-text",
                title=slide.title,
                point_claim_ids=point_ids,
                frame_asset_ids=[slide.asset_id],
                citation_ids=[frame_evidence],
                notes_claim_ids=[],
                notes="[evidence-only] v2 text has no claim refs",
            )
        )
    migrated = SlideSpecV3(
        schema_version="3.0",
        spec_revision=1,
        producer_version="yt2class-v2-migration",
        analysis_mode="frames",
        quality_status=quality_status,
        source=SlideSource(
            source_id="src-migrated",
            kind=spec.source.kind,
            title=spec.source.title,
            media_path=spec.source.media_path,
            sha256=spec.source.sha256,
            duration_seconds=spec.source.duration_seconds,
            url=spec.source.url,
        ),
        assets=assets,
        evidence=evidence,
        claims=claims,
        slides=slides,
        theme=Theme(name="course-white-blue", font_family="Noto Sans CJK SC"),
        render_policy=RenderPolicy(
            max_pages=20,
            min_font_pt=18,
            aspect_ratio="16:9",
            overflow="repaginate-or-fail",
        ),
    )
    return report, migrated
