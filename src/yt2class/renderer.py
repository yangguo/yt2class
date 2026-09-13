"""Artifact Tool-backed PPTX renderer and structural validation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Mapping
from zipfile import BadZipFile, ZipFile


class RenderError(RuntimeError):
    """Raised when the Artifact Tool cannot produce a valid PPTX."""


_RENDERER_MJS = Path(__file__).with_name("assets") / "render_deck.mjs"

def _runtime_setup_candidates() -> list[Path]:
    """Find Codex presentation helpers without embedding a machine path.

    The helper is supplied by the desktop runtime and its cache directory is
    versioned and ephemeral. A normal installation can instead provide the
    explicit ``YT2CLASS_ARTIFACT_SETUP`` path.
    """

    cache_root = Path.home() / ".cache" / "codex-runtimes"
    if not cache_root.exists():
        return []
    return sorted(
        candidate
        for candidate in cache_root.glob("**/setup_artifact_tool_workspace.mjs")
        if candidate.is_file()
    )


def validate_pptx(path: Path) -> Path:
    """Check the minimum OOXML structure needed for a readable PowerPoint deck."""

    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        raise RenderError(f"PPTX output is missing or empty: {path}")
    try:
        with ZipFile(path) as archive:
            names = set(archive.namelist())
            required = {"[Content_Types].xml", "ppt/presentation.xml"}
            if not required.issubset(names):
                raise RenderError(f"output is not a PPTX ZIP: {path}")
            if not any(name.startswith("ppt/slides/slide") and name.endswith(".xml") for name in names):
                raise RenderError(f"PPTX contains no slides: {path}")
    except BadZipFile as error:
        raise RenderError(f"output is not a PPTX ZIP: {path}") from error
    return path


def _node_binary() -> str:
    configured = os.getenv("YT2CLASS_NODE")
    node = configured or shutil.which("node")
    if not node:
        raise RenderError("Node.js is required for Artifact Tool PPTX rendering")
    return node


def _setup_script() -> Path:
    configured = os.getenv("YT2CLASS_ARTIFACT_SETUP")
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return candidate
        raise RenderError(
            f"configured Artifact Tool setup helper is unavailable: {candidate}"
        )
    candidates = _runtime_setup_candidates()
    if candidates:
        return candidates[-1]
    raise RenderError(
        "Artifact Tool setup helper is unavailable; set YT2CLASS_ARTIFACT_SETUP "
        "to setup_artifact_tool_workspace.mjs"
    )


def _clear_preview_dir(preview_dir: Path) -> None:
    preview_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("slide-*.png", "slide-*.layout.json", "deck-montage.webp"):
        for path in preview_dir.glob(pattern):
            if path.is_file():
                path.unlink()


def render_deck(
    spec: Mapping[str, object] | Path,
    output_path: Path,
    *,
    preview_dir: Path | None = None,
) -> Path:
    """Render a validated JSON-like deck specification through Artifact Tool."""

    output_path = Path(output_path).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(spec, Path):
        spec_payload = json.loads(spec.read_text(encoding="utf-8"))
    else:
        spec_payload = dict(spec)

    with tempfile.TemporaryDirectory(prefix="yt2class-artifact-", dir=str(output_path.parent)) as temp_dir:
        workspace = Path(temp_dir)
        spec_path = workspace / "deck.json"
        spec_path.write_text(json.dumps(spec_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        renderer_path = workspace / "render_deck.mjs"
        shutil.copy2(_RENDERER_MJS, renderer_path)
        node = _node_binary()
        setup = _setup_script()
        setup_process = subprocess.run(
            [node, str(setup), "--workspace", str(workspace)],
            capture_output=True,
            text=True,
            check=False,
        )
        if setup_process.returncode != 0:
            detail = (setup_process.stderr or setup_process.stdout).strip()
            raise RenderError(f"Artifact Tool setup failed: {detail}")

        command = [
            node,
            str(renderer_path),
            "--spec",
            str(spec_path),
            "--output",
            str(output_path),
        ]
        if preview_dir is not None:
            preview_dir = Path(preview_dir).expanduser().resolve()
            _clear_preview_dir(preview_dir)
            command.extend(["--preview-dir", str(preview_dir)])
        process = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            detail = (process.stderr or process.stdout).strip()
            raise RenderError(f"Artifact Tool renderer failed: {detail}")
    return validate_pptx(output_path)
