"""Cache invalidation, digests, and validated stage hits."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from yt2class.domain.run_manifest import StageName
from yt2class.orchestration.cache import (
    CacheCorrupt,
    clear_stage_cache,
    invalidate_from,
    stage_cache_hit,
)
from yt2class.orchestration.manifest_io import reset_stages, stage_record
from yt2class.stages.review import document_digest


def model_digest(model: BaseModel) -> str:
    return document_digest(model)


def invalidate_stage_tree(run_root: Path, stage: StageName) -> None:
    clear_stage_cache(run_root, stage)
    for downstream in invalidate_from(stage):
        clear_stage_cache(run_root, downstream)


def refresh_stage_keys(
    manifest,
    run_root: Path,
    stage: StageName,
    new_key: str,
    *,
    save,
):
    """If the stage key changed, wipe downstream cache dirs and reset manifest rows."""

    record = stage_record(manifest, stage)
    if record.cache_key and record.cache_key != new_key:
        invalidate_stage_tree(run_root, stage)
        downstream = (stage,) + tuple(invalidate_from(stage))
        manifest = reset_stages(manifest, downstream)
        save(manifest)
    return manifest


def validated_cache_hit(
    run_root: Path,
    stage: StageName,
    cache_key: str,
    validator: Callable[[Path], None],
) -> bool:
    if not stage_cache_hit(run_root, stage, cache_key):
        return False
    try:
        validator(run_root / "cache" / stage / cache_key)
    except Exception:
        invalidate_stage_tree(run_root, stage)
        marker = run_root / "cache" / stage / cache_key / ".complete"
        marker.unlink(missing_ok=True)
        return False
    return True


__all__ = [
    "invalidate_stage_tree",
    "model_digest",
    "refresh_stage_keys",
    "validated_cache_hit",
]
