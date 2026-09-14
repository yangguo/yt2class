"""PptxGenJS integration: package assertions and layout fixtures."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from zipfile import ZipFile

import pytest

from yt2class.adapters.render.pptxgenjs import PptxGenJsRenderer, renderer_root
from yt2class.stages.bind_spec import bind_editorial_plan
from yt2class.stages.render import render_bound_spec
from tests.helpers.m4 import seed_lecture_run

pytestmark = pytest.mark.skipif(
    not shutil.which("node") or not (renderer_root() / "node_modules").is_dir(),
    reason="Node renderer toolchain unavailable",
)


def _pptx_text(archive: ZipFile) -> str:
    chunks: list[str] = []
    for name in archive.namelist():
        if name.endswith(".xml") and "ppt/" in name:
            chunks.append(archive.read(name).decode("utf-8", errors="ignore"))
    return "\n".join(chunks)


def test_renderer_resolves_bundled_package_without_repo_root():
    root = renderer_root()
    assert (root / "package.json").is_file()
    assert (root / "node_modules" / "pptxgenjs").is_dir()


def test_end_to_end_bind_render_sources(tmp_path: Path):
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
    stage = render_bound_spec(bound, workspace, request_id="render-e2e", preview_policy="off")
    pptx = workspace.safe_path(stage.pptx_path)
    assert pptx.stat().st_size > 5000
    with ZipFile(pptx) as archive:
        blob = _pptx_text(archive)
        assert bound.spec.slides[0].title in blob
        assert any(name.startswith("ppt/notesSlides/") for name in archive.namelist())
    sources = workspace.safe_path(stage.provenance.sources_path)
    payload = json.loads(sources.read_text(encoding="utf-8"))
    assert len(payload["pages"]) == len(bound.spec.slides)
    assert stage.report.render_complete is True


def test_fixture_spec_renders_all_layouts(tmp_path: Path):
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
    from yt2class.stages.render import spec_digest
    from yt2class.adapters.render.base import RenderRequest

    request = RenderRequest(
        request_id="render-layouts",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/layouts.pptx",
        preview_policy="off",
    )
    report = renderer.render(request)
    assert report.page_map
    assert report.visual_qa in {"passed", "unavailable", "failed"}


def test_atomic_write_failure_surfaces(tmp_path: Path, monkeypatch):
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

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    from yt2class.stages.render import spec_digest
    from yt2class.adapters.render.base import RenderRequest

    request = RenderRequest(
        request_id="render-fail",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/fail.pptx",
        preview_policy="off",
    )
    with pytest.raises(Exception):
        renderer.render(request)
