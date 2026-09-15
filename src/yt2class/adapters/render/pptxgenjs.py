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

RENDER_REPORT_REL = "delivery/render-report.json"


def renderer_root() -> Path:
    """Prefer wheel-bundled renderer; fall back to repo ``renderer/`` in dev checkouts."""

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


def _pptx_has_hyperlinks(archive: ZipFile, *, youtube: bool) -> bool:
    if not youtube:
        return True
    for name in archive.namelist():
        if "/slides/_rels/" in name and name.endswith(".xml.rels"):
            body = archive.read(name).decode("utf-8", errors="ignore")
            if "hyperlink" in body.lower() or "youtube.com" in body:
                return True
    return False


def validate_pptx_package(path: Path, *, spec: SlideSpecV3) -> None:
    """Raise when the PPTX package fails structural delivery checks."""

    if not path.is_file() or path.stat().st_size == 0:
        raise RenderError(f"PPTX output is missing or empty: {path}")
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            if not required.issubset(names):
                raise RenderError(f"output is not a PPTX ZIP: {path}")
            slide_xml = [
                name for name in names if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            ]
            if len(slide_xml) != len(spec.slides):
                raise RenderError(
                    f"slide count mismatch: pptx={len(slide_xml)} spec={len(spec.slides)}"
                )
            if any(slide.notes for slide in spec.slides) and not any(
                name.startswith("ppt/notesSlides/") for name in names
            ):
                raise RenderError("expected speaker notes parts in PPTX package")
            youtube = spec.source.kind == "youtube" and bool(spec.source.url)
            if youtube and not _pptx_has_hyperlinks(archive, youtube=True):
                raise RenderError("PPTX is missing hyperlink relationships for YouTube seek links")
    except BadZipFile as error:
        raise RenderError(f"output is not a PPTX ZIP: {path}") from error


def persist_render_report(run_root: Path, report: RenderReport, *, relative_path: str = RENDER_REPORT_REL) -> Path:
    dest = run_root / relative_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".partial")
    tmp.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(dest)
    return dest


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
        fd, tmp_name = tempfile.mkstemp(suffix=".pptx", dir=output.parent)
        os.close(fd)
        tmp = Path(tmp_name)
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
        node_modules = root / "node_modules"
        if node_modules.is_dir():
            env["NODE_PATH"] = str(node_modules)
        completed = subprocess.run(
            cmd,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            tmp.unlink(missing_ok=True)
            raise RenderError(f"PptxGenJS renderer failed: {detail}")

        try:
            validate_pptx_package(tmp, spec=spec)
            qa = run_visual_qa(
                tmp,
                spec=spec,
                preview_policy=request.preview_policy,
                run_root=run_root,
            )
            if request.preview_policy == "required" and qa.visual_qa != "passed":
                raise RenderError(
                    f"preview_policy=required but visual QA is {qa.visual_qa!r}: "
                    + "; ".join(qa.layout_issues)
                )
        except RenderError:
            tmp.unlink(missing_ok=True)
            raise

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

        render_complete = qa.visual_qa in {"passed", "unavailable"}
        report = RenderReport(
            schema_version="1.0",
            spec_revision=spec.spec_revision,
            request_id=request.request_id,
            pptx_path=request.output_path,
            pptx_sha256=pptx_hash,
            page_map=page_map,
            render_complete=render_complete,
            visual_qa=qa.visual_qa,
            layout_issues=list(qa.layout_issues),
        )
        persist_render_report(run_root, report)
        return report


__all__ = [
    "PptxGenJsRenderer",
    "RENDER_REPORT_REL",
    "persist_render_report",
    "renderer_root",
    "validate_pptx_package",
]
