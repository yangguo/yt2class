"""Persisted build inputs for resume and cache invalidation."""

from __future__ import annotations

import json
from pathlib import Path

from typing import Literal

from pydantic import Field

from yt2class.config import BuildSource
from yt2class.domain.common import StrictModel
from yt2class.orchestration.cache import file_sha256


class RunRequestRecord(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    url: str | None = None
    video_path: str | None = None
    subtitles_path: str | None = None
    subtitles_sha256: str | None = None
    review_revision: int = Field(default=0, ge=0)
    source_id: str | None = None


def subtitles_digest(path: Path | None) -> str | None:
    if path is None:
        return None
    if not path.is_file():
        return None
    return file_sha256(path)


def record_from_build(build: BuildSource, *, review_revision: int = 0) -> RunRequestRecord:
    video_path = str(build.video.resolve()) if build.video else None
    sub_path = str(build.subtitles.resolve()) if build.subtitles else None
    return RunRequestRecord(
        url=build.url,
        video_path=video_path,
        subtitles_path=sub_path,
        subtitles_sha256=subtitles_digest(build.subtitles),
        review_revision=review_revision,
        source_id=build.source_id,
    )


def request_path(run_root: Path) -> Path:
    return run_root / "metadata" / "run-request.json"


def load_run_request(run_root: Path) -> RunRequestRecord | None:
    path = request_path(run_root)
    if not path.is_file():
        return None
    try:
        return RunRequestRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_run_request(run_root: Path, record: RunRequestRecord) -> Path:
    path = request_path(run_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def merge_build_for_resume(
    run_root: Path,
    build: BuildSource,
) -> tuple[BuildSource, RunRequestRecord, bool]:
    """Return merged build, stored record, and whether subtitles (or source) changed."""

    stored = load_run_request(run_root)
    incoming = record_from_build(build, review_revision=stored.review_revision if stored else 0)
    if stored is None:
        save_run_request(run_root, incoming)
        return build, incoming, False

    changed = (
        stored.subtitles_sha256 != incoming.subtitles_sha256
        or stored.url != incoming.url
        or stored.video_path != incoming.video_path
    )
    merged = BuildSource(
        url=build.url or stored.url,
        video=build.video or (Path(stored.video_path) if stored.video_path else None),
        subtitles=build.subtitles
        or (Path(stored.subtitles_path) if stored.subtitles_path else None),
        run_id=build.run_id or stored.source_id,
        source_id=build.source_id or stored.source_id,
    )
    record = record_from_build(merged, review_revision=stored.review_revision)
    if changed:
        save_run_request(run_root, record)
    return merged, record, changed


def bump_review_revision(run_root: Path) -> int:
    from yt2class.orchestration.cache_policy import invalidate_stage_tree

    stored = load_run_request(run_root) or RunRequestRecord()
    updated = stored.model_copy(update={"review_revision": stored.review_revision + 1})
    save_run_request(run_root, updated)
    invalidate_stage_tree(run_root, "bind_spec")
    return updated.review_revision


__all__ = [
    "RunRequestRecord",
    "bump_review_revision",
    "load_run_request",
    "merge_build_for_resume",
    "record_from_build",
    "request_path",
    "save_run_request",
    "subtitles_digest",
]
