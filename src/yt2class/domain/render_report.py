"""RenderReport: persisted renderer and visual-QA outcome."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, model_validator

from yt2class.domain.common import Digest, Identifier, RelativePath, StrictModel, unique_ids

VisualQa = Literal["passed", "failed", "unavailable"]


class PageMapEntry(StrictModel):
    page_id: Identifier
    pptx_slide_index: int = Field(ge=0)
    layout: str = Field(min_length=1, max_length=40)


class RenderReport(StrictModel):
    schema_version: Literal["1.0"]
    spec_revision: int = Field(ge=1)
    request_id: Identifier
    pptx_path: RelativePath
    pptx_sha256: Digest
    page_map: list[PageMapEntry] = Field(min_length=1)
    render_complete: bool
    visual_qa: VisualQa
    layout_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_pages(self) -> Self:
        unique_ids(self.page_map, attr="page_id", label="render page")
        unique_ids(self.page_map, attr="pptx_slide_index", label="pptx slide index")
        return self
