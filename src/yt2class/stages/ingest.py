"""Ingest YouTube or local video inputs into a source manifest."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
from typing import Callable

from yt2class.adapters.ffmpeg import MediaProbe, probe_media
from yt2class.adapters.ytdlp import YtDlpDownload, download_video
from yt2class.domain.source import (
    SourceInput,
    SourceInputError,
    SourceManifest,
    content_sha256,
)
from yt2class.orchestration.workspace import Workspace, WorkspacePathError


class IngestError(RuntimeError):
    """Raised when a source cannot be published as a valid manifest."""


@dataclass(frozen=True)
class IngestResult:
    manifest: SourceManifest
    media_path: Path
    source_hash: str
    probe: MediaProbe | object
    manifest_path: Path


Downloader = Callable[..., YtDlpDownload]
Prober = Callable[..., MediaProbe | object]


def _ensure_inside(workspace: Workspace, path: Path) -> Path:
    try:
        resolved = path.expanduser().resolve(strict=True)
    except OSError as error:
        raise IngestError(f"ingested media does not exist: {path}") from error
    if not resolved.is_file() or resolved.stat().st_size == 0:
        raise IngestError(f"ingested media is empty or not a file: {path}")
    if not resolved.is_relative_to(workspace.root):
        raise WorkspacePathError(f"ingested media escapes workspace: {path}")
    return resolved


def _copy_local(source: Path, workspace: Workspace, source_hash: str) -> Path:
    destination = workspace.safe_path(
        f"media/{source_hash[:16]}{source.suffix.lower()}",
        create_parent=True,
    )
    if source.resolve() != destination.resolve():
        temporary = workspace.safe_path(
            f"tmp/{source_hash[:16]}.part{source.suffix.lower()}",
            create_parent=True,
        )
        temporary.unlink(missing_ok=True)
        shutil.copy2(source, temporary)
        temporary.replace(destination)
    return _ensure_inside(workspace, destination)


def _source_id(source: SourceInput, source_hash: str, requested: str | None) -> str:
    if requested:
        return requested
    if source.kind == "youtube":
        return f"yt-{source.video_id}"
    return f"src-{source_hash[:16]}"


def _title(source: SourceInput, media_path: Path, downloaded: YtDlpDownload | None) -> str:
    if source.kind == "youtube":
        if downloaded is None or downloaded.info_path is None:
            raise IngestError("YouTube download is missing info JSON metadata")
        try:
            payload = json.loads(downloaded.info_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise IngestError("YouTube info JSON metadata is invalid")
            metadata_id = str(payload.get("id") or "").strip()
            if metadata_id != source.video_id:
                raise IngestError(
                    "YouTube info JSON metadata id does not match the requested video id"
                )
            title = str(payload.get("title") or "").strip()
            if title:
                return title[:160]
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise IngestError("YouTube info JSON metadata is invalid") from error
        raise IngestError("YouTube info JSON metadata has no title")
    return media_path.stem[:160]


def _relative_media_path(workspace: Workspace, media_path: Path) -> str:
    try:
        return media_path.resolve(strict=True).relative_to(workspace.root).as_posix()
    except ValueError as error:
        raise WorkspacePathError(f"media path is outside workspace: {media_path}") from error


def _build_manifest(
    source: SourceInput,
    workspace: Workspace,
    media_path: Path,
    probe: MediaProbe | object,
    source_hash: str,
    downloaded: YtDlpDownload | None,
    source_id: str | None,
) -> SourceManifest:
    duration = float(getattr(probe, "duration_seconds"))
    streams = list(getattr(probe, "streams"))
    timebase = str(getattr(probe, "timebase"))
    manifest = SourceManifest(
        schema_version="1.0",
        source_id=_source_id(source, source_hash, source_id),
        kind=source.kind,
        title=_title(source, media_path, downloaded),
        media_path=_relative_media_path(workspace, media_path),
        sha256=source_hash,
        duration_seconds=duration,
        url=source.normalized_value if source.kind == "youtube" else None,
        video_id=source.video_id if source.kind == "youtube" else None,
        streams=streams,
        timebase=timebase,
        local_mode=source.local_mode,
        reference_path=(str(Path(source.value).resolve()) if source.kind == "local" and source.local_mode == "reference" else None),
        transformations=[],
    )
    return manifest


def _manifest_path(workspace: Workspace) -> Path:
    return workspace.safe_path("metadata/source-manifest.json", create_parent=True)


def _write_manifest_atomic(workspace: Workspace, manifest: SourceManifest) -> Path:
    destination = _manifest_path(workspace)
    temporary = workspace.safe_path("tmp/.source-manifest.json.tmp", create_parent=True)
    payload = manifest.model_dump(mode="json")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise IngestError(f"cannot publish source manifest: {destination}") from error
    return destination


def resolve_manifest_media(manifest: SourceManifest, workspace: Workspace) -> Path:
    """Resolve and verify the immutable media path represented by a manifest."""

    if manifest.kind == "local" and manifest.local_mode == "reference":
        if manifest.reference_path is None:
            raise IngestError("reference manifest has no reference_path")
        try:
            media_path = Path(manifest.reference_path).expanduser().resolve(strict=True)
        except OSError as error:
            raise IngestError(f"reference media does not exist: {manifest.reference_path}") from error
    else:
        try:
            media_path = workspace.safe_path(manifest.media_path).resolve(strict=True)
        except (OSError, WorkspacePathError) as error:
            raise IngestError(f"manifest media path is not available: {manifest.media_path}") from error
        if not media_path.is_relative_to(workspace.root):
            raise WorkspacePathError(f"manifest media escapes workspace: {manifest.media_path}")
    if not media_path.is_file() or media_path.stat().st_size == 0:
        raise IngestError(f"manifest media is empty or not a file: {media_path}")
    try:
        actual_hash = content_sha256(media_path)
    except SourceInputError as error:
        raise IngestError(f"cannot hash manifest media: {media_path}") from error
    if actual_hash != manifest.sha256:
        raise IngestError(f"manifest media hash does not match: {media_path}")
    return media_path


def _ingest_source_locked(
    source: SourceInput | str | Path,
    workspace: Workspace,
    *,
    source_id: str | None = None,
    downloader: Downloader = download_video,
    prober: Prober = probe_media,
) -> IngestResult:
    """Publish one immutable media artifact and its validated SourceManifest.

    A local ``copy`` is atomically copied into the run workspace.  A local
    ``reference`` is probed in place and records the absolute dependency path.
    YouTube downloads are always published below ``workspace.media_dir`` and
    retain the filename returned by yt-dlp.
    """

    manifest_path = _manifest_path(workspace)
    had_previous_manifest = manifest_path.is_file()
    try:
        return _ingest_source_unlocked(
            source,
            workspace,
            source_id=source_id,
            downloader=downloader,
            prober=prober,
            manifest_path=manifest_path,
        )
    except Exception:
        # A published manifest is the last complete result.  Keep it visible
        # while a rerun is being prepared; the atomic writer replaces it only
        # after the new bytes have been fully written and fsynced.  On the
        # first run, remove any unexpected newly-created manifest on failure.
        if not had_previous_manifest:
            manifest_path.unlink(missing_ok=True)
        raise


def _ingest_source_unlocked(
    source: SourceInput | str | Path,
    workspace: Workspace,
    *,
    source_id: str | None,
    downloader: Downloader,
    prober: Prober,
    manifest_path: Path,
) -> IngestResult:
    if not isinstance(source, SourceInput):
        source = SourceInput.from_value(source)
    downloaded: YtDlpDownload | None = None
    if source.kind == "youtube":
        download_dir = workspace.safe_path("media", create_parent=True).resolve(strict=True)
        downloaded = downloader(source, download_dir)
        media_path = _ensure_inside(workspace, Path(downloaded.media_path))
    elif source.local_mode == "copy":
        original = Path(source.value).resolve(strict=True)
        source_hash = content_sha256(original)
        media_path = _copy_local(original, workspace, source_hash)
    else:
        media_path = Path(source.value).resolve(strict=True)
        if not media_path.is_file() or media_path.stat().st_size == 0:
            raise SourceInputError(f"local source is empty or not a file: {media_path}")

    # Probe before constructing/returning the manifest so failures cannot be
    # represented as complete ingestion output.
    probe = prober(media_path)
    source_hash = content_sha256(media_path)
    manifest_media_path = media_path if source.kind == "youtube" or source.local_mode == "copy" else workspace.media_dir / f"reference-{source_hash[:16]}{media_path.suffix.lower()}"
    if source.kind == "local" and source.local_mode == "reference":
        # Keep a run-relative logical path in SourceManifest while the actual
        # bytes remain at reference_path.
        manifest_media_path.parent.mkdir(parents=True, exist_ok=True)
        relative_media_path = manifest_media_path.relative_to(workspace.root).as_posix()
        manifest = SourceManifest(
            schema_version="1.0",
            source_id=_source_id(source, source_hash, source_id),
            kind="local",
            title=_title(source, media_path, None),
            media_path=relative_media_path,
            sha256=source_hash,
            duration_seconds=float(getattr(probe, "duration_seconds")),
            streams=list(getattr(probe, "streams")),
            timebase=str(getattr(probe, "timebase")),
            local_mode="reference",
            reference_path=str(media_path),
            transformations=[],
        )
    else:
        manifest = _build_manifest(
            source,
            workspace,
            media_path,
            probe,
            source_hash,
            downloaded,
            source_id,
        )
    _write_manifest_atomic(workspace, manifest)
    return IngestResult(
        manifest=manifest,
        media_path=media_path,
        source_hash=source_hash,
        probe=probe,
        manifest_path=manifest_path,
    )


def ingest_source(
    source: SourceInput | str | Path,
    workspace: Workspace,
    *,
    source_id: str | None = None,
    downloader: Downloader = download_video,
    prober: Prober = probe_media,
    lock: bool = True,
) -> IngestResult:
    """Ingest one source under the run's single-writer lock.

    Pass ``lock=False`` only when the caller already owns
    :meth:`Workspace.write_lock`; this keeps nested calls from deadlocking.
    """

    if not lock:
        return _ingest_source_locked(
            source,
            workspace,
            source_id=source_id,
            downloader=downloader,
            prober=prober,
        )
    with workspace.write_lock():
        return _ingest_source_locked(
            source,
            workspace,
            source_id=source_id,
            downloader=downloader,
            prober=prober,
        )


def ingest_source_locked(
    source: SourceInput | str | Path,
    workspace: Workspace,
    *,
    source_id: str | None = None,
    downloader: Downloader = download_video,
    prober: Prober = probe_media,
) -> IngestResult:
    """Explicit alias for callers that already hold ``workspace.write_lock``."""

    return ingest_source(
        source,
        workspace,
        source_id=source_id,
        downloader=downloader,
        prober=prober,
        lock=False,
    )


__all__ = [
    "IngestError",
    "IngestResult",
    "ingest_source",
    "ingest_source_locked",
    "resolve_manifest_media",
]
