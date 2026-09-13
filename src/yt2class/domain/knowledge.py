"""KnowledgeDocument: units, claims, relations, and uncertainty."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Identifier,
    Seconds,
    StrictModel,
    UnitInterval,
    unique_ids,
    validate_half_open,
)

UnitKind = Literal["concept", "example", "procedure", "comparison", "warning", "recap"]
ClaimStatus = Literal["draft", "supported", "contradicted", "insufficient", "unresolved"]
ClaimModality = Literal["audio", "visual", "both"]
ClaimProvenance = Literal["source", "generated-practice"]
RelationKind = Literal["supports", "contrasts", "prerequisite", "step_before"]
UncertaintyKind = Literal["conflict", "missing_audio", "unreadable_text", "missing_step"]
DesiredModality = Literal["frame", "clip", "audio"]


class KnowledgeClaim(StrictModel):
    id: Identifier
    text: str = Field(min_length=1, max_length=4000)
    evidence_ids: list[Identifier] = Field(min_length=1, max_length=20)
    status: ClaimStatus
    qualifiers: list[str] = Field(default_factory=list, max_length=20)
    modality: ClaimModality = "both"
    provenance: ClaimProvenance = "source"


class KnowledgeRelation(StrictModel):
    from_id: Identifier
    to_id: Identifier
    kind: RelationKind


class VisualCandidate(StrictModel):
    frame_id: Identifier
    relevance: UnitInterval
    legibility: UnitInterval
    selection_reason: str = Field(min_length=1, max_length=240)


class Uncertainty(StrictModel):
    kind: UncertaintyKind
    start_seconds: Seconds
    end_seconds: Seconds
    note: str = Field(min_length=1, max_length=400)

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="uncertainty")
        return self


class KnowledgeEvidenceRequest(StrictModel):
    start_seconds: Seconds
    end_seconds: Seconds
    reason: str = Field(min_length=1, max_length=240)
    desired_modality: DesiredModality

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="evidence request")
        return self


class KnowledgeUnit(StrictModel):
    id: Identifier
    topic_id: Identifier
    segment_ids: list[Identifier] = Field(min_length=1, max_length=20)
    start_seconds: Seconds
    end_seconds: Seconds
    kind: UnitKind
    claims: list[KnowledgeClaim] = Field(min_length=1)
    relations: list[KnowledgeRelation] = Field(default_factory=list)
    visual_candidates: list[VisualCandidate] = Field(default_factory=list)
    uncertainty: list[Uncertainty] = Field(default_factory=list)
    evidence_requests: list[KnowledgeEvidenceRequest] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_unit(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="knowledge unit")
        unique_ids(self.claims, label="claim")
        return self


class KnowledgeDocument(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    units: list[KnowledgeUnit] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_document(self) -> Self:
        unique_ids(self.units, label="knowledge unit")
        claim_ids = unique_ids(self.iter_claims(), label="claim")
        for unit in self.units:
            for relation in unit.relations:
                if relation.from_id not in claim_ids and relation.from_id != unit.id:
                    raise ValueError(f"unknown relation from_id {relation.from_id!r}")
                if relation.to_id not in claim_ids and relation.to_id not in {
                    other.id for other in self.units
                }:
                    raise ValueError(f"unknown relation to_id {relation.to_id!r}")
        return self

    def iter_claims(self) -> list[KnowledgeClaim]:
        return [claim for unit in self.units for claim in unit.claims]
