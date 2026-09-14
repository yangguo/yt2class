"""Render stage: bound SlideSpec 3.0 to PPTX plus provenance."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from yt2class.adapters.render.base import PreviewPolicy, RenderRequest, Renderer
from yt2class.adapters.render.pptxgenjs import PptxGenJsRenderer
from yt2class.domain.render_report import RenderReport
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.orchestration.workspace import Workspace
from yt2class.provenance import ProvenanceResult, build_provenance
from yt2class.stages.bind_spec import BindResult, validate_bound_assets


@dataclass(frozen=True)
class RenderStageResult:
    report: RenderReport
    provenance: ProvenanceResult
    pptx_path: str


def spec_digest(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def render_bound_spec(
    bind: BindResult,
    workspace: Workspace,
    *,
    request_id: str,
    preview_policy: PreviewPolicy = "optional",
    output_path: str = "delivery/lesson.pptx",
    renderer: Renderer | None = None,
) -> RenderStageResult:
    spec_path = workspace.safe_path(bind.spec_path)
    validate_bound_assets(bind.spec, workspace.root)
    request = RenderRequest(
        request_id=request_id,
        spec_path=bind.spec_path,
        spec_digest=spec_digest(spec_path),
        run_root=str(workspace.root),
        output_path=output_path,
        preview_policy=preview_policy,
    )
    engine = renderer or PptxGenJsRenderer()
    report = engine.render(request)
    provenance = build_provenance(bind.spec, workspace=workspace, page_map=report.page_map)
    return RenderStageResult(report=report, provenance=provenance, pptx_path=output_path)


__all__ = ["RenderStageResult", "render_bound_spec", "spec_digest"]
