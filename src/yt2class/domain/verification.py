"""VerificationReport: claim verdicts and remaining review work."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Identifier, StrictModel, unique_ids

Verdict = Literal["supported", "contradicted", "insufficient"]
QualityMode = Literal["strict", "draft", "evidence-only"]


class ClaimVerdict(StrictModel):
    claim_id: Identifier
    verdict: Verdict
    supporting_ids: list[Identifier] = Field(default_factory=list)
    contradicting_ids: list[Identifier] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=400)


class VerificationReport(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    quality_mode: QualityMode
    verdicts: list[ClaimVerdict] = Field(default_factory=list)
    structural_errors: list[str] = Field(default_factory=list)
    pending_review: list[Identifier] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_verdicts(self) -> Self:
        unique_ids(self.verdicts, attr="claim_id", label="verdict claim")
        if self.quality_mode == "strict":
            unresolved = [
                item.claim_id
                for item in self.verdicts
                if item.verdict != "supported"
            ] + list(self.pending_review)
            if unresolved or self.structural_errors:
                raise ValueError(
                    "strict verification cannot contain unresolved claims, "
                    "pending review items, or structural errors"
                )
        return self
