"""Controlled review bundle. Not a published 3.0 schema; hashes bind artifacts."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Digest, Identifier, StrictModel, unique_ids
from yt2class.domain.editorial import EditorialPlan, PageIntent
from yt2class.domain.media_audit import MediaPrivacyAudit
from yt2class.domain.verification import VerificationReport

ReviewOpName = Literal["edit_copy", "pick_asset", "delete", "lock", "reorder"]
ALLOWED_REVIEW_OPS: tuple[ReviewOpName, ...] = (
    "edit_copy",
    "pick_asset",
    "delete",
    "lock",
    "reorder",
)


class ReviewOp(StrictModel):
    op: ReviewOpName
    page_id: Identifier | None = None
    title: str | None = Field(default=None, min_length=1, max_length=80)
    notes: str | None = Field(default=None, max_length=4000)
    body_points: list[str] | None = Field(default=None, max_length=4)
    frame_ids: list[Identifier] | None = Field(default=None, max_length=3)
    order: list[Identifier] | None = None

    @model_validator(mode="after")
    def check_op(self) -> Self:
        if self.op == "reorder":
            if not self.order:
                raise ValueError("reorder requires an order list")
        elif self.page_id is None:
            raise ValueError(f"{self.op} requires page_id")
        if self.op == "edit_copy" and self.title is None and self.notes is None and self.body_points is None:
            raise ValueError("edit_copy requires title, notes, or body_points")
        if self.op == "pick_asset" and not self.frame_ids:
            raise ValueError("pick_asset requires frame_ids")
        return self


class ReviewEdits(StrictModel):
    revision: int = Field(ge=1)
    baseline_hashes: dict[str, Digest]
    ops: list[ReviewOp] = Field(min_length=1)


class ReviewPageView(StrictModel):
    page: PageIntent
    claim_texts: list[str] = Field(default_factory=list)
    transcript_excerpts: list[str] = Field(default_factory=list)
    frame_ids: list[Identifier] = Field(default_factory=list)
    frame_asset_paths: list[str] = Field(default_factory=list)
    time_links: list[str] = Field(default_factory=list)
    selection_reason: str = Field(default="", max_length=400)


class ReviewBundle(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    revision: int = Field(ge=1)
    baseline_hashes: dict[str, Digest]
    plan: EditorialPlan
    report: VerificationReport
    pages: list[ReviewPageView] = Field(min_length=1)
    omitted_topics: list[str] = Field(default_factory=list)
    allowed_ops: list[ReviewOpName] = Field(default_factory=lambda: list(ALLOWED_REVIEW_OPS))
    allowed_frame_ids: list[Identifier] = Field(default_factory=list)
    media_privacy: MediaPrivacyAudit | None = None

    @model_validator(mode="after")
    def check_bundle(self) -> Self:
        unique_ids((item.page for item in self.pages), label="review page")
        unknown_ops = [op for op in self.allowed_ops if op not in ALLOWED_REVIEW_OPS]
        if unknown_ops:
            raise ValueError(f"unknown review ops {unknown_ops}")
        required = {"knowledge", "editorial", "transcript", "visual"}
        missing = required - set(self.baseline_hashes)
        if missing:
            raise ValueError(f"review bundle missing baseline hashes {sorted(missing)}")
        verified = [item.page for item in self.pages if item.page.quality_label == "verified"]
        if verified:
            if self.report.quality_mode != "strict":
                raise ValueError(
                    "verified review pages require a strict verification report, "
                    f"got {self.report.quality_mode!r}"
                )
            supported = {
                item.claim_id for item in self.report.verdicts if item.verdict == "supported"
            }
            unverified = sorted(
                {claim_id for page in verified for claim_id in page.claim_ids} - supported
            )
            if unverified:
                raise ValueError(f"verified review pages cite unverified claims {unverified}")
        return self


class StaleReviewError(ValueError):
    """Raised when a review file no longer matches the current artifact set."""


class IllegalReviewOpError(ValueError):
    """Raised when a review tries an operation outside the allowed set."""
