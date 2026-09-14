"""Persist and update RunManifest on disk."""

from __future__ import annotations

from pathlib import Path
import uuid

from yt2class.domain.run_manifest import (
    RunManifest,
    RunUsage,
    StageName,
    StageRecord,
    StageStatus,
    ToolVersions,
)
from yt2class.domain.slide_spec_v3 import AnalysisMode, QualityStatus
from yt2class.orchestration.cache import atomic_write_json, load_validated_json, CacheCorrupt

ALL_STAGES: tuple[StageName, ...] = (
    "ingest",
    "extract_evidence",
    "outline",
    "analyze_segments",
    "reduce_knowledge",
    "edit_deck",
    "verify_claims",
    "bind_spec",
    "render",
)


def new_run_id() -> str:
    return f"run-{uuid.uuid4().hex[:12]}"


def initial_manifest(
    *,
    run_id: str,
    source_id: str,
    analysis_mode: AnalysisMode = "frames",
    quality_status: QualityStatus = "incomplete",
    producer_version: str = "yt2class-m5",
) -> RunManifest:
    stages = [StageRecord(name=name, status="pending", cache_key=None, error=None) for name in ALL_STAGES]
    return RunManifest(
        schema_version="1.0",
        run_id=run_id,
        source_id=source_id,
        analysis_mode=analysis_mode,
        quality_status=quality_status,
        stages=stages,
        tool_versions=ToolVersions(producer_version=producer_version),
        usage=RunUsage(
            input_tokens=0,
            output_tokens=0,
            image_count=0,
            video_seconds=0.0,
            estimated_usd=0.0,
        ),
        errors=[],
    )


def manifest_path(run_root: Path) -> Path:
    return run_root / "manifest.json"


def load_manifest(run_root: Path) -> RunManifest | None:
    path = manifest_path(run_root)
    if not path.is_file():
        return None
    try:
        return load_validated_json(path, RunManifest)
    except CacheCorrupt:
        return None


def save_manifest(run_root: Path, manifest: RunManifest) -> Path:
    path = manifest_path(run_root)
    atomic_write_json(path, manifest.model_dump(mode="json"))
    return path


def stage_record(manifest: RunManifest, name: StageName) -> StageRecord:
    for record in manifest.stages:
        if record.name == name:
            return record
    raise KeyError(name)


def set_stage_status(
    manifest: RunManifest,
    name: StageName,
    status: StageStatus,
    *,
    cache_key: str | None = None,
    error: str | None = None,
) -> RunManifest:
    updated: list[StageRecord] = []
    for record in manifest.stages:
        if record.name != name:
            updated.append(record)
            continue
        updated.append(
            record.model_copy(
                update={
                    "status": status,
                    "cache_key": cache_key if cache_key is not None else record.cache_key,
                    "error": error,
                }
            )
        )
    return manifest.model_copy(update={"stages": updated})


def reset_stages(manifest: RunManifest, names: tuple[StageName, ...]) -> RunManifest:
    targets = set(names)
    updated = [
        record.model_copy(update={"status": "pending", "cache_key": None, "error": None})
        if record.name in targets
        else record
        for record in manifest.stages
    ]
    return manifest.model_copy(update={"stages": updated})


def merge_usage(manifest: RunManifest, **fields: float | int) -> RunManifest:
    usage = manifest.usage.model_copy(
        update={
            key: getattr(manifest.usage, key) + fields.get(key, 0)
            for key in ("input_tokens", "output_tokens", "image_count", "video_seconds", "estimated_usd")
        }
    )
    return manifest.model_copy(update={"usage": usage})


__all__ = [
    "ALL_STAGES",
    "initial_manifest",
    "load_manifest",
    "manifest_path",
    "merge_usage",
    "new_run_id",
    "reset_stages",
    "save_manifest",
    "set_stage_status",
    "stage_record",
]
