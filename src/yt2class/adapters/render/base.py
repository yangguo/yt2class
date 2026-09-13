"""Renderer request contract. Path/hash binding stays in later binder tests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import Field

from yt2class.domain.common import Digest, Identifier, RelativePath, StrictModel
from yt2class.domain.render_report import PageMapEntry, RenderReport

PreviewPolicy = Literal["required", "optional", "off"]


class RenderRequest(StrictModel):
    request_id: Identifier
    spec_path: RelativePath
    run_root: str = Field(min_length=1, max_length=500)
    output_path: RelativePath
    preview_policy: PreviewPolicy = "optional"


class Renderer(ABC):
    def __init__(self) -> None:
        self._reports: dict[str, RenderReport] = {}

    def render(self, request: RenderRequest) -> RenderReport:
        cached = self._reports.get(request.request_id)
        if cached is not None:
            return cached
        report = self._render(request)
        if report.request_id != request.request_id:
            raise ValueError("render report request_id does not match the request")
        self._reports[request.request_id] = report
        return report

    @abstractmethod
    def _render(self, request: RenderRequest) -> RenderReport:
        raise NotImplementedError


class FakeRenderer(Renderer):
    """Contract double. Does not write PPTX or invoke PptxGenJS."""

    def _render(self, request: RenderRequest) -> RenderReport:
        visual_qa = "unavailable" if request.preview_policy != "required" else "passed"
        return RenderReport(
            schema_version="1.0",
            spec_revision=1,
            request_id=request.request_id,
            pptx_path=request.output_path,
            pptx_sha256="e" * 64,
            page_map=[PageMapEntry(page_id="page-1", pptx_slide_index=0, layout="image-text")],
            render_complete=True,
            visual_qa=visual_qa,
        )
