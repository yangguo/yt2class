"""EditorialPlan: page intents. Must not contain source paths or hashes."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Identifier, StrictModel, unique_ids

PageType = Literal["cover", "content", "summary", "quiz"]
PageLayout = Literal["image-text", "text", "comparison", "sequence"]
DeckOrder = Literal["chronological", "teaching"]


class PageIntent(StrictModel):
    id: Identifier
    type: PageType
    layout: PageLayout | None = None
    title: str = Field(min_length=1, max_length=80)
    claim_ids: list[Identifier] = Field(default_factory=list, max_length=8)
    frame_ids: list[Identifier] = Field(default_factory=list, max_length=3)
    notes: str = Field(default="", max_length=4000)
    selection_reason: str = Field(min_length=1, max_length=400)


class Omission(StrictModel):
    topic_id: Identifier | None = None
    claim_id: Identifier | None = None
    reason: str = Field(min_length=1, max_length=400)


class EditorialPlan(StrictModel):
    schema_version: Literal["1.0"]
    source_id: Identifier
    target_pages: int = Field(ge=4, le=100)
    max_pages: int = Field(ge=4, le=100)
    order: DeckOrder = "chronological"
    pages: list[PageIntent] = Field(min_length=1)
    omissions: list[Omission] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_pages(self) -> Self:
        if self.max_pages < self.target_pages:
            raise ValueError("max_pages must be >= target_pages")
        unique_ids(self.pages, label="editorial page")
        if len(self.pages) > self.max_pages:
            raise ValueError("editorial pages exceed max_pages")
        return self
