from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from yt2class.adapters.render.qa import run_visual_qa
from yt2class.adapters.render.pptxgenjs import PptxGenJsRenderer, renderer_root
from yt2class.stages.bind_spec import bind_editorial_plan
from yt2class.stages.render import render_bound_spec, spec_digest
from yt2class.adapters.render.base import RenderRequest
from tests.helpers.m4 import seed_lecture_run

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not (renderer_root() / "node_modules").is_dir(),
    reason="Node renderer toolchain unavailable",
)


def test_visual_qa_unavailable_when_libreoffice_missing(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=source,
        transcript=transcript,
        visual=visual,
        workspace=workspace,
    )
    renderer = PptxGenJsRenderer()
    request = RenderRequest(
        request_id="qa-off",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/qa.pptx",
        preview_policy="off",
    )
    report = renderer.render(request)
    assert report.visual_qa == "unavailable"
    assert report.render_complete is True
    outcome = run_visual_qa(
        workspace.safe_path(report.pptx_path),
        spec=bound.spec,
        preview_policy="off",
        run_root=workspace.root,
    )
    assert outcome.visual_qa == "unavailable"
