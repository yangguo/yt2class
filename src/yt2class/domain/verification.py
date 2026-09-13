"""VerificationReport: claim verdicts and remaining review work."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Identifier, StrictModel, unique_ids

Verdict = Literal["supported", "contradicted", "insufficient"]
QualityMode = Literal["strict", "draft", "evidence-only"]
CheckKind = Literal[
    "number",
    "negation",
    "condition",
    "proper_name",
    "translation",
    "step",
    "image_text",
    "generated_practice",
    "unknown_ref",
    "contradiction",
    "grounding",
]


class ClaimVerdict(StrictModel):
    claim_id: Identifier
    verdict: Verdict
    supporting_ids: list[Identifier] = Field(default_factory=list)
    contradicting_ids: list[Identifier] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def supported_requires_evidence(self) -> Self:
        if self.verdict == "supported" and not self.supporting_ids:
            raise ValueError("supported verdict requires supporting evidence ids")
        return self


class HumanSample(StrictModel):
    """A claim reserved for human sampling. Model agreement is never sufficient."""

    claim_id: Identifier
    reason: str = Field(min_length=1, max_length=240)
    check_kinds: list[CheckKind] = Field(default_factory=list, max_length=12)


class VerificationReport(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    quality_mode: QualityMode
    verdicts: list[ClaimVerdict] = Field(default_factory=list)
    structural_errors: list[str] = Field(default_factory=list)
    pending_review: list[Identifier] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    repaired_claim_ids: list[Identifier] = Field(default_factory=list)
    removed_from_formal: list[Identifier] = Field(default_factory=list)
    human_samples: list[HumanSample] = Field(default_factory=list)
    # Model-vs-model agreement is never a pass criterion; this is always True
    # when any source claim remains for a human to sample.
    human_sampling_required: bool = False

    @model_validator(mode="after")
    def check_verdicts(self) -> Self:
        unique_ids(self.verdicts, attr="claim_id", label="verdict claim")
        unique_ids(self.human_samples, attr="claim_id", label="human sample")
        verdict_ids = {item.claim_id for item in self.verdicts}
        for sample in self.human_samples:
            if sample.claim_id not in verdict_ids:
                raise ValueError(f"human sample cites unknown claim {sample.claim_id!r}")
        if self.quality_mode == "evidence-only" and any(
            item.verdict == "supported" for item in self.verdicts
        ):
            raise ValueError("evidence-only verification cannot contain supported claims")
        if self.quality_mode == "strict":
            if not self.verdicts:
                raise ValueError("strict verification requires a closed non-empty claim set")
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
