"""PptxGenJS integration: package assertions and layout fixtures."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from yt2class.adapters.render.base import RenderError, RenderRequest
from yt2class.adapters.render.pptxgenjs import (
    PptxGenJsRenderer,
    RENDER_REPORT_REL,
    renderer_root,
    validate_pptx_package,
)
from yt2class.stages.bind_spec import bind_editorial_plan
from yt2class.stages.render import render_bound_spec, spec_digest
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


def _has_hyperlink_rels(archive: ZipFile) -> bool:
    for name in archive.namelist():
        if "/slides/_rels/" in name and name.endswith(".xml.rels"):
            body = archive.read(name).decode("utf-8", errors="ignore")
            if "hyperlink" in body.lower() or "youtube.com" in body:
                return True
    return False


def test_renderer_resolves_bundled_package_without_repo_root():
    root = renderer_root()
    assert (root / "package.json").is_file()
    assert (root / "node_modules" / "pptxgenjs").is_dir()


def test_end_to_end_bind_render_sources(tmp_path: Path):
    workspace, outcome, source, transcript, visual, _ = seed_lecture_run(tmp_path)
    youtube = source.model_copy(
        update={
            "kind": "youtube",
            "url": "https://www.youtube.com/watch?v=abc123_",
            "video_id": "abc123_",
        }
    )
    bound = bind_editorial_plan(
        plan=outcome.plan,
        knowledge=outcome.knowledge,
        report=outcome.report,
        source=youtube,
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
        assert _has_hyperlink_rels(archive)
    sources = workspace.safe_path(stage.provenance.sources_path)
    payload = json.loads(sources.read_text(encoding="utf-8"))
    assert len(payload["pages"]) == len(bound.spec.slides)
    assert stage.report.render_complete is True
    report_path = workspace.safe_path(RENDER_REPORT_REL)
    assert report_path.is_file()


def test_renderer_uses_each_page_timestamp_without_carrying_it_to_summary(tmp_path: Path):
    root = renderer_root()
    output = tmp_path / "page-times.pptx"
    script = """
import { renderSpec } from "./src/render.mjs";
const spec = {
  source: { kind: "local", media_path: "media/lesson.mp4" },
  theme: { font_family: "Arial" },
  claims: [],
  assets: [],
  evidence: [
    { id: "ev-later", kind: "transcript", asset_id: "asset-later", start_seconds: 257, end_seconds: 260, text: "later", origin: "sidecar" },
    { id: "ev-earlier", kind: "transcript", asset_id: "asset-earlier", start_seconds: 99, end_seconds: 102, text: "earlier", origin: "sidecar" }
  ],
  slides: [
    { id: "page-later", type: "content", layout: "text", title: "Later source", point_claim_ids: [], bullets: ["Later"], citation_ids: ["ev-later"], notes: "" },
    { id: "page-earlier", type: "content", layout: "text", title: "Earlier source", point_claim_ids: [], bullets: ["Earlier"], citation_ids: ["ev-earlier"], notes: "" },
    { id: "page-summary", type: "summary", title: "Summary", claim_ids: ["claim-a", "claim-b"], bullets: ["A", "B"], citation_ids: ["ev-later", "ev-earlier"], notes: "" },
    { id: "page-unanchored", type: "content", layout: "text", title: "No source time", point_claim_ids: [], bullets: ["No time"], citation_ids: ["ev-missing"], notes: "" }
  ]
};
await renderSpec(spec, process.cwd(), process.argv[1]);
"""
    subprocess.run(
        ["node", "--input-type=module", "-e", script, str(output)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    with ZipFile(output) as archive:
        blob = _pptx_text(archive)
    assert blob.count("04:17") == 1
    assert blob.count("01:39") == 1
    assert "来源 00:00" not in blob


def test_preview_required_without_libreoffice_fails_closed(tmp_path: Path):
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
    dest = workspace.safe_path("delivery/required.pptx")
    request = RenderRequest(
        request_id="render-required",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/required.pptx",
        preview_policy="required",
    )
    with pytest.raises(RenderError, match="preview_policy=required"):
        renderer.render(request)
    assert not dest.exists()


def test_package_validation_failure_leaves_destination_absent(tmp_path: Path, monkeypatch):
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
    dest = workspace.safe_path("delivery/bad.pptx")

    def boom(_path, *, spec):
        raise RenderError("injected package failure")

    monkeypatch.setattr(
        "yt2class.adapters.render.pptxgenjs.validate_pptx_package",
        boom,
    )
    request = RenderRequest(
        request_id="render-bad-package",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/bad.pptx",
        preview_policy="off",
    )
    with pytest.raises(RenderError, match="injected package failure"):
        renderer.render(request)
    assert not dest.exists()


def test_atomic_write_failure_leaves_destination_absent(tmp_path: Path, monkeypatch):
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
    dest = workspace.safe_path("delivery/fail.pptx")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", boom)
    request = RenderRequest(
        request_id="render-fail",
        spec_path=bound.spec_path,
        spec_digest=spec_digest(workspace.safe_path(bound.spec_path)),
        run_root=str(workspace.root),
        output_path="delivery/fail.pptx",
        preview_policy="off",
    )
    with pytest.raises(OSError, match="disk full"):
        renderer.render(request)
    assert not dest.exists()
