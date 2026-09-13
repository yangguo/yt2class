"""SlideSpec 3.0: binder output and the only trusted renderer input.

v2 lives in ``yt2class.slide_spec`` and is unchanged. Path/hash binding against
the filesystem is deferred to binder tests; this module checks document shape
and in-document reference closure.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    PositiveSeconds,
    RelativePath,
    Seconds,
    StrictModel,
    unique_ids,
    validate_half_open,
)
from yt2class.domain.visual import BoundingBox

AnalysisMode = Literal["frames", "native-video", "hybrid"]
QualityStatus = Literal["verified", "review_required", "incomplete", "evidence-only"]
AssetRole = Literal["frame", "clip", "audio", "transcript"]
ClaimVerdict = Literal["supported", "contradicted", "insufficient"]
ClaimProvenance = Literal["source", "generated-practice"]
TranscriptOrigin = Literal["sidecar", "manual-caption", "auto-caption", "asr"]
ThemeName = Literal["course-white-blue"]
AspectRatio = Literal["16:9"]
OverflowPolicy = Literal["repaginate-or-fail"]
SlideType = Literal["cover", "content", "summary", "quiz"]
ContentLayout = Literal["image-text", "text", "comparison", "sequence"]
QuizKind = Literal["source-question", "generated-practice"]


class SlideSource(StrictModel):
    source_id: Identifier
    kind: Literal["youtube", "local"]
    title: str = Field(min_length=1, max_length=160)
    media_path: RelativePath
    sha256: Digest
    duration_seconds: PositiveSeconds
    url: str | None = None

    @model_validator(mode="after")
    def check_url(self) -> Self:
        if self.kind == "youtube" and self.url is None:
            raise ValueError("youtube source requires url")
        if self.kind == "local" and self.url is not None:
            raise ValueError("local source must not supply a remote url")
        return self


class SlideAsset(StrictModel):
    id: Identifier
    role: AssetRole
    path: RelativePath
    sha256: Digest
    mime_type: str = Field(min_length=1, max_length=64)
    timestamp_seconds: Seconds | None = None
    start_seconds: Seconds | None = None
    end_seconds: Seconds | None = None
    derived_from: Identifier | None = None
    transformations: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def check_role(self) -> Self:
        if self.role == "frame":
            if self.timestamp_seconds is None:
                raise ValueError("frame asset requires timestamp_seconds")
            if self.start_seconds is not None or self.end_seconds is not None:
                raise ValueError("frame asset must not have a time range")
        elif self.role in {"clip", "audio", "transcript"}:
            if self.start_seconds is None or self.end_seconds is None:
                raise ValueError(
                    f"{self.role} asset requires start_seconds and end_seconds"
                )
            validate_half_open(self.start_seconds, self.end_seconds, label="asset")
            if self.timestamp_seconds is not None:
                raise ValueError(f"{self.role} asset must not have timestamp_seconds")
        return self


class FrameEvidence(StrictModel):
    id: Identifier
    kind: Literal["frame"]
    asset_id: Identifier
    timestamp_seconds: Seconds


class TranscriptEvidence(StrictModel):
    id: Identifier
    kind: Literal["transcript"]
    asset_id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds
    text: str = Field(min_length=1, max_length=4000)
    origin: TranscriptOrigin

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="transcript evidence")
        return self


class ClipEvidence(StrictModel):
    id: Identifier
    kind: Literal["clip"]
    asset_id: Identifier
    start_seconds: Seconds
    end_seconds: Seconds

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="clip evidence")
        return self


class OcrEvidence(StrictModel):
    id: Identifier
    kind: Literal["ocr"]
    parent_frame_evidence_id: Identifier
    bbox: BoundingBox
    text: str = Field(min_length=1, max_length=4000)
    engine: str = Field(min_length=1, max_length=64)


EvidenceItem = Annotated[
    FrameEvidence | TranscriptEvidence | ClipEvidence | OcrEvidence,
    Field(discriminator="kind"),
]


class SlideClaim(StrictModel):
    id: Identifier
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=20)
    verdict: ClaimVerdict
    provenance: ClaimProvenance


class SequenceStep(StrictModel):
    asset_id: Identifier
    claim_ids: list[Identifier] = Field(min_length=1, max_length=4)
    caption: str | None = Field(default=None, max_length=160)


class QuizQuestion(StrictModel):
    prompt: str = Field(min_length=1, max_length=240)
    kind: QuizKind
    answer_claim_ids: list[Identifier] = Field(default_factory=list, max_length=4)


class SlidePage(StrictModel):
    id: Identifier
    type: SlideType
    title: str = Field(min_length=1, max_length=80)
    layout: ContentLayout | None = None
    subtitle: str | None = Field(default=None, max_length=160)
    source_id: Identifier | None = None
    hero_asset_id: Identifier | None = None
    point_claim_ids: list[Identifier] = Field(default_factory=list, max_length=4)
    frame_asset_ids: list[Identifier] = Field(default_factory=list, max_length=3)
    captions: list[str] = Field(default_factory=list, max_length=3)
    steps: list[SequenceStep] = Field(default_factory=list)
    claim_ids: list[Identifier] = Field(default_factory=list, max_length=4)
    questions: list[QuizQuestion] = Field(default_factory=list)
    citation_ids: list[Identifier] = Field(default_factory=list)
    notes_claim_ids: list[Identifier] = Field(default_factory=list)
    continuation_of: Identifier | None = None
    notes: str = Field(default="", max_length=8000)

    @model_validator(mode="after")
    def check_layout_contract(self) -> Self:
        common_fields = {
            "id",
            "type",
            "title",
            "citation_ids",
            "notes_claim_ids",
            "continuation_of",
            "notes",
        }

        def has_value(value: object) -> bool:
            if value is None:
                return False
            if isinstance(value, (list, tuple, dict, set)):
                return bool(value)
            if isinstance(value, str):
                return bool(value)
            return True

        allowed_fields = set(common_fields)
        if self.type == "cover":
            allowed_fields.update({"subtitle", "source_id", "hero_asset_id"})
            if self.source_id is None:
                raise ValueError("cover slide requires source_id")
        elif self.type == "content":
            allowed_fields.add("layout")
            if self.layout is None:
                raise ValueError("content slide requires layout")
            if self.layout != "sequence":
                allowed_fields.add("point_claim_ids")
                if not 1 <= len(self.point_claim_ids) <= 4:
                    raise ValueError("content slide requires 1-4 point_claim_ids")
            if self.layout == "image-text":
                allowed_fields.add("frame_asset_ids")
                if len(self.frame_asset_ids) != 1:
                    raise ValueError("image-text layout requires exactly 1 frame")
            if self.layout == "text" and self.frame_asset_ids:
                raise ValueError("text layout requires 0 frames")
            if self.layout == "comparison":
                allowed_fields.update({"frame_asset_ids", "captions"})
                if len(self.frame_asset_ids) != 2 or len(self.captions) != 2:
                    raise ValueError("comparison layout requires 2 frames and 2 captions")
            if self.layout == "sequence":
                allowed_fields.add("steps")
                if self.point_claim_ids:
                    raise ValueError("sequence layout must use steps, not point_claim_ids")
                if not 2 <= len(self.steps) <= 3:
                    raise ValueError("sequence layout requires 2-3 steps")
        elif self.type == "summary":
            allowed_fields.add("claim_ids")
            if not 1 <= len(self.claim_ids) <= 4:
                raise ValueError("summary slide requires 1-4 claim_ids")
        elif self.type == "quiz":
            allowed_fields.add("questions")
            if not 1 <= len(self.questions) <= 3:
                raise ValueError("quiz slide requires 1-3 questions")

        for field_name in (
            "layout",
            "subtitle",
            "source_id",
            "hero_asset_id",
            "point_claim_ids",
            "frame_asset_ids",
            "captions",
            "steps",
            "claim_ids",
            "questions",
        ):
            if field_name not in allowed_fields and has_value(getattr(self, field_name)):
                raise ValueError(
                    f"{self.type} slide does not allow field {field_name!r}"
                )
        return self


class Theme(StrictModel):
    name: ThemeName
    font_family: str = Field(min_length=1, max_length=80)


class RenderPolicy(StrictModel):
    max_pages: int = Field(ge=4, le=100)
    min_font_pt: float = Field(gt=0, allow_inf_nan=False)
    aspect_ratio: AspectRatio
    overflow: OverflowPolicy


class SlideSpecV3(StrictModel):
    schema_version: Literal["3.0"]
    spec_revision: int = Field(ge=1)
    producer_version: str = Field(min_length=1, max_length=80)
    analysis_mode: AnalysisMode
    quality_status: QualityStatus
    source: SlideSource
    assets: list[SlideAsset] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    claims: list[SlideClaim] = Field(default_factory=list)
    slides: list[SlidePage] = Field(min_length=1)
    theme: Theme
    render_policy: RenderPolicy

    @model_validator(mode="after")
    def check_references(self) -> Self:
        assets = unique_ids(self.assets, label="asset")
        evidence = unique_ids(self.evidence, label="evidence")
        claims = unique_ids(self.claims, label="claim")
        slides = unique_ids(self.slides, label="slide")
        duration = self.source.duration_seconds

        for asset in self.assets:
            if asset.timestamp_seconds is not None and asset.timestamp_seconds >= duration:
                raise ValueError("asset timestamp must be < source duration")
            if asset.end_seconds is not None and asset.end_seconds > duration:
                raise ValueError("asset range exceeds source duration")
            if asset.derived_from is not None and asset.derived_from not in assets:
                raise ValueError(f"unknown derived_from {asset.derived_from!r}")

        for item in self.evidence:
            if isinstance(item, FrameEvidence):
                asset = assets.get(item.asset_id)
                if asset is None or asset.role != "frame":
                    raise ValueError("frame evidence requires a frame asset")
                if item.timestamp_seconds >= duration:
                    raise ValueError("frame timestamp must be < source duration")
                if asset.timestamp_seconds != item.timestamp_seconds:
                    raise ValueError("frame evidence timestamp must match frame asset")
            elif isinstance(item, TranscriptEvidence):
                asset = assets.get(item.asset_id)
                if asset is None or asset.role != "transcript":
                    raise ValueError("transcript evidence requires a transcript asset")
                if item.end_seconds > duration:
                    raise ValueError("transcript evidence exceeds source duration")
                if (asset.start_seconds, asset.end_seconds) != (
                    item.start_seconds,
                    item.end_seconds,
                ):
                    raise ValueError("transcript evidence range must match transcript asset")
            elif isinstance(item, ClipEvidence):
                asset = assets.get(item.asset_id)
                if asset is None or asset.role != "clip":
                    raise ValueError("clip evidence requires a clip asset")
                if item.end_seconds > duration:
                    raise ValueError("clip evidence exceeds source duration")
                if (asset.start_seconds, asset.end_seconds) != (
                    item.start_seconds,
                    item.end_seconds,
                ):
                    raise ValueError("clip evidence range must match clip asset")
            elif isinstance(item, OcrEvidence):
                parent = evidence.get(item.parent_frame_evidence_id)
                if parent is None or not isinstance(parent, FrameEvidence):
                    raise ValueError("ocr evidence requires a frame parent")

        for claim in self.claims:
            missing = set(claim.evidence_ids) - set(evidence)
            if missing:
                raise ValueError(f"unknown claim evidence {sorted(missing)}")
            if self.quality_status == "verified" and claim.verdict != "supported":
                raise ValueError("verified SlideSpec may only contain supported claims")
            if self.quality_status == "evidence-only" and claim.verdict == "supported":
                raise ValueError(
                    "evidence-only SlideSpec cannot mark claims supported; "
                    "v2 text without claim refs stays unresolved/evidence-only"
                )

        page_ids = set(slides)
        for slide in self.slides:
            if slide.continuation_of is not None and slide.continuation_of not in page_ids:
                raise ValueError(f"unknown continuation_of {slide.continuation_of!r}")
            if slide.source_id is not None and slide.source_id != self.source.source_id:
                raise ValueError("cover source_id must match SlideSpec source")
            for asset_id in slide.frame_asset_ids:
                asset = assets.get(asset_id)
                if asset is None:
                    raise ValueError(f"unknown slide frame asset {asset_id!r}")
                if asset.role != "frame":
                    raise ValueError(f"slide frame asset {asset_id!r} must have role 'frame'")
            for step in slide.steps:
                asset = assets.get(step.asset_id)
                if asset is None:
                    raise ValueError(f"unknown sequence asset {step.asset_id!r}")
                if asset.role != "frame":
                    raise ValueError(
                        f"sequence asset {step.asset_id!r} must have role 'frame'"
                    )
                missing_claims = set(step.claim_ids) - set(claims)
                if missing_claims:
                    raise ValueError(f"unknown sequence claim {sorted(missing_claims)}")
            for field_name in ("point_claim_ids", "claim_ids", "notes_claim_ids"):
                missing_claims = set(getattr(slide, field_name)) - set(claims)
                if missing_claims:
                    raise ValueError(f"unknown {field_name} {sorted(missing_claims)}")
            for question in slide.questions:
                missing_claims = set(question.answer_claim_ids) - set(claims)
                if missing_claims:
                    raise ValueError(f"unknown quiz answer claim {sorted(missing_claims)}")
            missing_citations = set(slide.citation_ids) - set(evidence)
            if missing_citations:
                raise ValueError(f"unknown citation {sorted(missing_citations)}")
            if slide.hero_asset_id is not None:
                hero = assets.get(slide.hero_asset_id)
                if hero is None:
                    raise ValueError(f"unknown hero asset {slide.hero_asset_id!r}")
                if hero.role != "frame":
                    raise ValueError(
                        f"hero asset {slide.hero_asset_id!r} must have role 'frame'"
                    )
        if len(self.slides) > self.render_policy.max_pages:
            raise ValueError("slides exceed render_policy.max_pages")
        return self
