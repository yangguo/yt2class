"""End-to-end YouTube lesson build orchestration."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from yt2class.deck import build_deck_spec
from yt2class.inputs import RunPaths, create_run_paths
from yt2class.llm import ModelConfig, select_plan_with_mode
from yt2class.media import MediaError, download_lesson, fetch_video_title
from yt2class.renderer import render_deck
from yt2class.scenes import FrameCandidate, extract_scene_candidates
from yt2class.subtitles import find_subtitle_file, load_vtt


class PipelineError(RuntimeError):
    """Raised when one lesson cannot complete all stages."""


@dataclass(frozen=True)
class BuildResult:
    url: str
    run_dir: Path
    pptx_path: Path
    manifest_path: Path
    deck_spec_path: Path
    candidate_count: int
    selected_count: int


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_source_notes(
    path: Path,
    *,
    url: str,
    source_video: Path,
    subtitle_path: Path | None,
    selection_mode: str,
    selection_note: str | None,
    spec: dict[str, Any],
) -> None:
    """Persist human-readable provenance beside the editable deck spec."""

    lines = [
        "[Sources]",
        f"Source video URL: {url}",
        f"Downloaded source: {source_video}",
        f"Subtitle file: {subtitle_path if subtitle_path else '(none)'}",
        f"Selection mode: {selection_mode}",
    ]
    if selection_note:
        lines.append(f"Selection note: {selection_note}")
    lines.extend(["", "Original frames embedded in the PPTX:"])
    for slide in spec.get("slides", []):
        lines.extend(
            [
                f"- {slide['frame_id']} @ {float(slide['timestamp']):.3f}s",
                f"  path: {slide['frame_path']}",
                f"  sha256: {slide['source_sha256']}",
            ]
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _resume_result(paths: RunPaths, url: str) -> BuildResult | None:
    if not paths.pptx_path.exists() or not paths.manifest_path.exists():
        return None
    try:
        manifest = json.loads(paths.manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return BuildResult(
        url=url,
        run_dir=paths.root,
        pptx_path=paths.pptx_path,
        manifest_path=paths.manifest_path,
        deck_spec_path=paths.deck_spec_path,
        candidate_count=int(manifest.get("candidate_frame_count", 0)),
        selected_count=int(manifest.get("selected_frame_count", len(manifest.get("slides", [])))),
    )


def build_lesson(
    url: str,
    output_dir: Path,
    *,
    selection_file: Path | None = None,
    max_slides: int = 12,
    force: bool = False,
    model_config: ModelConfig | None = None,
    preview: bool = True,
) -> BuildResult:
    """Build one lesson and retain every intermediate artifact under its run dir."""

    if max_slides < 1:
        raise PipelineError("max_slides must be at least 1")
    paths = create_run_paths(Path(output_dir), url)
    if not force:
        resumed = _resume_result(paths, url)
        if resumed is not None:
            return resumed

    source_video = paths.source_video
    if not source_video.exists() or source_video.stat().st_size == 0:
        try:
            source_video = Path(download_lesson(url, paths.media_dir))
        except MediaError as error:
            raise PipelineError(f"download stage failed: {error}") from error

    try:
        course_title = fetch_video_title(url)
    except MediaError:
        course_title = url

    subtitle_path = find_subtitle_file(paths.media_dir)
    cues = load_vtt(subtitle_path) if subtitle_path else []
    candidates = extract_scene_candidates(source_video, paths.frames_dir)
    if not candidates:
        raise PipelineError("scene stage produced no candidate frames")

    selection = select_plan_with_mode(
        course_title,
        candidates,
        cues,
        selection_file=selection_file,
        max_slides=max_slides,
        config=model_config,
    )
    plan = selection.plan
    spec = build_deck_spec(url, plan, candidates, source_video=source_video)
    _write_json(paths.deck_spec_path, spec)
    source_notes_path = paths.analysis_dir / "source-notes.txt"
    _write_source_notes(
        source_notes_path,
        url=url,
        source_video=source_video,
        subtitle_path=subtitle_path,
        selection_mode=selection.mode,
        selection_note=selection.note,
        spec=spec,
    )
    preview_dir = paths.analysis_dir / "previews" if preview else None
    try:
        render_deck(spec, paths.pptx_path, preview_dir=preview_dir)
    except Exception as error:
        raise PipelineError(f"PPTX render stage failed: {error}") from error

    manifest_slides = [
        {
            "frame_id": slide["frame_id"],
            "timestamp": slide["timestamp"],
            "source_frame": slide["frame_path"],
            "source_frame_sha256": slide["source_sha256"],
        }
        for slide in spec["slides"]
    ]
    manifest = {
        "schema_version": 1,
        "url": url,
        "title": plan.title,
        "source_video": str(source_video),
        "subtitle_file": str(subtitle_path) if subtitle_path else None,
        "candidate_frame_count": len(candidates),
        "selected_frame_count": len(manifest_slides),
        "selection_mode": selection.mode,
        "selection_note": selection.note,
        "pptx": str(paths.pptx_path),
        "source_notes": str(source_notes_path),
        "slides": manifest_slides,
        "plan": plan.model_dump(mode="json"),
    }
    _write_json(paths.manifest_path, manifest)
    return BuildResult(
        url=url,
        run_dir=paths.root,
        pptx_path=paths.pptx_path,
        manifest_path=paths.manifest_path,
        deck_spec_path=paths.deck_spec_path,
        candidate_count=len(candidates),
        selected_count=len(manifest_slides),
    )


def build_batch(
    urls: list[str],
    output_dir: Path,
    *,
    selection_file: Path | None = None,
    max_slides: int = 12,
    force: bool = False,
    model_config: ModelConfig | None = None,
    preview: bool = True,
) -> list[BuildResult]:
    """Build URLs sequentially so each run remains easy to inspect and resume."""

    return [
        build_lesson(
            url,
            output_dir,
            selection_file=selection_file,
            max_slides=max_slides,
            force=force,
            model_config=model_config,
            preview=preview,
        )
        for url in urls
    ]
