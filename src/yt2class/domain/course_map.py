"""CourseMap: outline hypotheses. Speculative fields are not source claims."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Identifier,
    Seconds,
    StrictModel,
    unique_ids,
    validate_half_open,
)

TopicRelationKind = Literal["prerequisite", "follows", "contrasts"]


class Topic(StrictModel):
    id: Identifier
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=400)
    start_seconds: Seconds
    end_seconds: Seconds
    evidence_ids: list[Identifier] = Field(default_factory=list, max_length=50)
    speculative: bool = False

    @model_validator(mode="after")
    def check_range(self) -> Self:
        validate_half_open(self.start_seconds, self.end_seconds, label="topic")
        return self


class TopicRelation(StrictModel):
    from_topic_id: Identifier
    to_topic_id: Identifier
    kind: TopicRelationKind


class CourseMap(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    topics: list[Topic] = Field(default_factory=list)
    relations: list[TopicRelation] = Field(default_factory=list)
    unverified_guesses: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def check_topics(self) -> Self:
        topics = unique_ids(self.topics, label="topic")
        for relation in self.relations:
            if relation.from_topic_id not in topics or relation.to_topic_id not in topics:
                raise ValueError("topic relation references an unknown topic id")
        return self
