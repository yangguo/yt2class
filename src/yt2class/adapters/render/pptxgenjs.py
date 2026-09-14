"""PptxGenJS renderer adapter for bound SlideSpec 3.0."""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any
from zipfile import BadZipFile, ZipFile

from yt2class.adapters.render.base import RenderError, RenderRequest, Renderer
from yt2class.adapters.render.qa import run_visual_qa
from yt2class.domain.render_report import PageMapEntry, RenderReport
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.stages.bind_spec import validate_bound_assets


def renderer_root() -> Path:
    env = os.getenv("YT2CLASS_RENDERER_ROOT")
    if env:
        candidate = Path(env).expanduser()
        if (candidate / "package.json").is_file():
            return candidate
        raise RenderError(f"YT2CLASS_RENDERER_ROOT is invalid: {candidate}")
    bundled = Path(__file__).resolve().parents[2] / "renderer_bundle"
    if (bundled / "package.json").is_file():
        return bundled
    repo = Path(__file__).resolve().parents[4] / "renderer"
    if (repo / "package.json").is_file():
        return repo
    raise RenderError("PptxGenJS renderer package is not installed")


def _node_binary() -> str:
    configured = os.getenv("YT2CLASS_NODE")
    node = configured or shutil.which("node")
    if not node:
        raise RenderError("Node.js is required for PptxGenJS rendering")
    return node


def _digest_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pptx_package(path: Path, *, spec: SlideSpecV3) -> list[str]:
    issues: list[str] = []
    if not path.is_file() or path.stat().st_size == 0:
        raise RenderError(f"PPTX output is missing or empty: {path}")
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            if not required.issubset(names):
                raise RenderError(f"output is not a PPTX ZIP: {path}")
            slide_xml = [name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
            if len(slide_xml) != len(spec.slides):
                issues.append(
                    f"slide count mismatch: pptx={len(slide_xml)} spec={len(spec.slides)}"
                )
            notes = [name for name in names if name.startswith("ppt/notesSlides/")]
            if any(slide.notes for slide in spec.slides) and not notes:
                issues.append("expected speaker notes parts in PPTX package")
    except BadZipFile as error:
        raise RenderError(f"output is not a PPTX ZIP: {path}") from error
    return issues


class PptxGenJsRenderer(Renderer):
    def _render(self, request: RenderRequest) -> RenderReport:
        run_root = Path(request.run_root).expanduser().resolve(strict=True)
        spec_path = run_root / request.spec_path
        if not spec_path.is_file():
            raise RenderError(f"SlideSpec missing: {spec_path}")
        if _digest_file(spec_path) != request.spec_digest:
            raise RenderError("spec_digest does not match SlideSpec file on disk")
        spec = SlideSpecV3.model_validate_json(spec_path.read_text(encoding="utf-8"))
        validate_bound_assets(spec, run_root)

        output = run_root / request.output_path
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkstemp(suffix=".pptx", dir=output.parent)[1])
        report_json = output.parent / f".{output.name}.render.json"

        root = renderer_root()
        entry = root / "src" / "render.mjs"
        if not entry.is_file():
            raise RenderError(f"renderer entry missing: {entry}")

        cmd = [
            _node_binary(),
            str(entry),
            "--spec",
            str(spec_path),
            "--run-root",
            str(run_root),
            "--output",
            str(tmp),
            "--report-path",
            str(report_json),
        ]
        env = os.environ.copy()
        node_path = root / "node_modules"
        if node_path.is_dir():
            env["NODE_PATH"] = str(node_path)
        completed = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise RenderError(f"PptxGenJS renderer failed: {detail}")

        package_issues = validate_pptx_package(tmp, spec=spec)
        tmp.replace(output)
        pptx_hash = _digest_file(output)

        page_map: list[PageMapEntry] = []
        if report_json.is_file():
            payload: dict[str, Any] = json.loads(report_json.read_text(encoding="utf-8"))
            page_map = [
                PageMapEntry(
                    page_id=item["page_id"],
                    pptx_slide_index=item["pptx_slide_index"],
                    layout=item["layout"],
                )
                for item in payload.get("page_map", [])
            ]
            report_json.unlink(missing_ok=True)
        if not page_map:
            page_map = [
                PageMapEntry(
                    page_id=slide.id,
                    pptx_slide_index=index,
                    layout=slide.layout or slide.type,
                )
                for index, slide in enumerate(spec.slides)
            ]

        qa = run_visual_qa(
            output,
            spec=spec,
            preview_policy=request.preview_policy,
            run_root=run_root,
        )
        layout_issues = list(package_issues) + list(qa.layout_issues)
        return RenderReport(
            schema_version="1.0",
            spec_revision=spec.spec_revision,
            request_id=request.request_id,
            pptx_path=request.output_path,
            pptx_sha256=pptx_hash,
            page_map=page_map,
            render_complete=True,
            visual_qa=qa.visual_qa,
            layout_issues=layout_issues,
        )


__all__ = ["PptxGenJsRenderer", "renderer_root", "validate_pptx_package"]
