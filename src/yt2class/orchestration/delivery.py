"""Production bind/render helpers for review apply and orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from yt2class.adapters.render.base import PreviewPolicy
from yt2class.domain.editorial import EditorialPlan
from yt2class.domain.knowledge import KnowledgeDocument
from yt2class.domain.source import SourceManifest
from yt2class.domain.transcript import TranscriptDocument
from yt2class.domain.verification import VerificationReport
from yt2class.domain.visual import VisualCatalogue
from yt2class.orchestration.workspace import Workspace
from yt2class.stages.bind_spec import BindResult, bind_editorial_plan
from yt2class.stages.render import RenderStageResult, render_bound_spec


def bind_plan_to_spec(
    *,
    plan: EditorialPlan,
    knowledge: KnowledgeDocument,
    report: VerificationReport,
    source: SourceManifest,
    transcript: TranscriptDocument,
    visual: VisualCatalogue,
    workspace: Workspace,
    analysis_mode: str = "frames",
) -> BindResult:
    return bind_editorial_plan(
        plan=plan,
        knowledge=knowledge,
        report=report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
        analysis_mode=analysis_mode,  # type: ignore[arg-type]
    )


def render_spec(
    bind: BindResult,
    workspace: Workspace,
    *,
    request_id: str,
    preview_policy: PreviewPolicy = "optional",
    output_path: str = "delivery/lesson.pptx",
) -> RenderStageResult:
    return render_bound_spec(
        bind,
        workspace,
        request_id=request_id,
        preview_policy=preview_policy,
        output_path=output_path,
    )


def binder_status_dict(bind: BindResult) -> dict[str, Any]:
    return {
        "status": "complete",
        "spec_path": bind.spec_path,
        "pages": len(bind.spec.slides),
    }


def renderer_status_dict(stage: RenderStageResult) -> dict[str, Any]:
    return {
        "status": "complete" if stage.report.render_complete else "degraded",
        "pptx_path": stage.pptx_path,
        "render_complete": stage.report.render_complete,
    }


__all__ = [
    "bind_plan_to_spec",
    "binder_status_dict",
    "render_spec",
    "renderer_status_dict",
]
