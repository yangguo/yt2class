"""Atomic stage cache: partial write, validate, rename; cache keys and invalidation."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, TypeVar

from pydantic import BaseModel, ValidationError

from yt2class.domain.run_manifest import StageName

TModel = TypeVar("TModel", bound=BaseModel)

STAGE_VERSIONS: dict[StageName, str] = {
    "ingest": "1",
    "extract_evidence": "1",
    "outline": "1",
    "analyze_segments": "1",
    "reduce_knowledge": "1",
    "edit_deck": "1",
    "verify_claims": "1",
    "bind_spec": "1",
    "render": "1",
}

STAGE_DOWNSTREAM: dict[StageName, tuple[StageName, ...]] = {
    "ingest": (
        "extract_evidence",
        "outline",
        "analyze_segments",
        "reduce_knowledge",
        "edit_deck",
        "verify_claims",
        "bind_spec",
        "render",
    ),
    "extract_evidence": (
        "outline",
        "analyze_segments",
        "reduce_knowledge",
        "edit_deck",
        "verify_claims",
        "bind_spec",
        "render",
    ),
    "outline": (
        "analyze_segments",
        "reduce_knowledge",
        "edit_deck",
        "verify_claims",
        "bind_spec",
        "render",
    ),
    "analyze_segments": (
        "reduce_knowledge",
        "edit_deck",
        "verify_claims",
        "bind_spec",
        "render",
    ),
    "reduce_knowledge": ("edit_deck", "verify_claims", "bind_spec", "render"),
    "edit_deck": ("verify_claims", "bind_spec", "render"),
    "verify_claims": ("bind_spec", "render"),
    "bind_spec": ("render",),
    "render": (),
}


class CacheError(RuntimeError):
    """Raised when cache IO or validation fails."""


class CacheCorrupt(CacheError):
    """Cached artifact failed validation or schema checks."""


def canonical_json(payload: Mapping[str, Any] | list[Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_parts(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\0")
    return hasher.hexdigest()[:32]


def compute_cache_key(
    stage: StageName,
    *,
    config_digest: str,
    input_hashes: Iterable[str],
    tool_versions: Mapping[str, str | None],
    review_revision: int | None = None,
) -> str:
    version = STAGE_VERSIONS[stage]
    ordered_inputs = "|".join(sorted(input_hashes))
    tools = canonical_json({key: tool_versions.get(key) for key in sorted(tool_versions)})
    extra = f"rev={review_revision}" if review_revision is not None else ""
    return digest_parts(stage, version, config_digest, ordered_inputs, tools, extra)


def invalidate_from(stage: StageName) -> tuple[StageName, ...]:
    return STAGE_DOWNSTREAM.get(stage, ())


@dataclass(frozen=True)
class AtomicWriteResult:
    path: Path
    sha256: str


def file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> AtomicWriteResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(data)
    if fsync:
        fd = os.open(partial, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    partial.replace(path)
    return AtomicWriteResult(path=path, sha256=hashlib.sha256(data).hexdigest())


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> AtomicWriteResult:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path, payload: Mapping[str, Any] | list[Any]) -> AtomicWriteResult:
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    return atomic_write_text(path, body)


def load_validated_json(
    path: Path,
    model: type[TModel],
    *,
    label: str | None = None,
) -> TModel:
    if not path.is_file():
        raise CacheCorrupt(f"missing cache artifact: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CacheCorrupt(f"corrupt cache JSON at {path}") from error
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        name = label or model.__name__
        raise CacheCorrupt(f"{name} schema mismatch at {path}: {error}") from error


def load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise CacheCorrupt(f"missing cache artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CacheCorrupt(f"corrupt cache JSON at {path}") from error
    if not isinstance(payload, dict):
        raise CacheCorrupt(f"expected object JSON at {path}")
    schema_version = payload.get("schema_version")
    if schema_version is None:
        raise CacheCorrupt(f"missing schema_version at {path}")
    return payload


def stage_artifact_path(run_root: Path, stage: StageName, cache_key: str, filename: str) -> Path:
    return run_root / "cache" / stage / cache_key / filename


def ensure_stage_marker(run_root: Path, stage: StageName, cache_key: str) -> Path:
    marker = stage_artifact_path(run_root, stage, cache_key, ".complete")
    return marker


def stage_cache_hit(
    run_root: Path,
    stage: StageName,
    cache_key: str,
    *,
    validator: Callable[[Path], None] | None = None,
) -> bool:
    marker = ensure_stage_marker(run_root, stage, cache_key)
    if not marker.is_file():
        return False
    if validator is None:
        return True
    try:
        validator(marker.parent)
    except CacheCorrupt:
        return False
    return True


def mark_stage_complete(run_root: Path, stage: StageName, cache_key: str) -> None:
    marker = ensure_stage_marker(run_root, stage, cache_key)
    atomic_write_text(marker, f"stage={stage}\nkey={cache_key}\n")


def clear_stage_cache(run_root: Path, stage: StageName) -> None:
    root = run_root / "cache" / stage
    if not root.exists():
        return
    for child in root.iterdir():
        if child.is_dir():
            for item in child.iterdir():
                item.unlink(missing_ok=True)
            child.rmdir()


__all__ = [
    "AtomicWriteResult",
    "CacheCorrupt",
    "CacheError",
    "STAGE_DOWNSTREAM",
    "STAGE_VERSIONS",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "canonical_json",
    "clear_stage_cache",
    "compute_cache_key",
    "digest_parts",
    "ensure_stage_marker",
    "file_sha256",
    "invalidate_from",
    "load_json_dict",
    "load_validated_json",
    "mark_stage_complete",
    "stage_artifact_path",
    "stage_cache_hit",
]
