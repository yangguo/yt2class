from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from yt2class.adapters.ytdlp import YtDlpDownload
from yt2class.domain.source import SourceInput, SourceManifest
from yt2class.orchestration.workspace import Workspace, WorkspacePathError
from yt2class.stages.ingest import (
    IngestError,
    ingest_source,
    resolve_manifest_media,
)


def _probe(path: Path) -> SimpleNamespace:
    from yt2class.domain.source import MediaStream

    return SimpleNamespace(
        path=path,
        duration_seconds=3.25,
        streams=(
            MediaStream(
                index=0,
                codec_type="video",
                codec_name="h264",
                fps=25.0,
                timebase="1/90000",
            ),
            MediaStream(index=1, codec_type="audio", codec_name="aac", timebase="1/48000"),
        ),
        rotation_degrees=0,
        fps=25.0,
        timebase="1/90000",
    )


def test_ingest_local_copy_is_immutable_and_writes_manifest(tmp_path: Path):
    original = tmp_path / "课程 sample.MKV"
    original.write_bytes(b"original media")
    original_bytes = original.read_bytes()
    workspace = Workspace.create(tmp_path / "runs", run_id="copy")

    result = ingest_source(
        SourceInput.from_value(original, local_mode="copy"),
        workspace,
        prober=_probe,
    )

    assert result.manifest.kind == "local"
    assert result.manifest.local_mode == "copy"
    assert result.manifest.media_path.startswith("media/")
    assert result.manifest.reference_path is None
    assert result.media_path.is_file()
    assert result.media_path.read_bytes() == original_bytes
    assert original.read_bytes() == original_bytes
    assert result.manifest.sha256 == result.source_hash
    assert result.manifest.duration_seconds == 3.25
    assert result.manifest.streams[0].fps == 25.0


def test_ingest_local_reference_does_not_copy_and_records_original_path(tmp_path: Path):
    original = tmp_path / "reference video.webm"
    original.write_bytes(b"reference media")
    workspace = Workspace.create(tmp_path / "runs", run_id="reference")

    result = ingest_source(
        SourceInput.from_value(original, local_mode="reference"),
        workspace,
        prober=_probe,
    )

    assert result.manifest.local_mode == "reference"
    assert result.manifest.reference_path == str(original.resolve())
    assert result.media_path == original.resolve()
    assert not (workspace.media_dir / original.name).exists()


def test_ingest_youtube_uses_actual_downloader_path_and_rejects_escape(tmp_path: Path):
    workspace = Workspace.create(tmp_path / "runs", run_id="youtube")
    downloaded = workspace.media_dir / "日语 lesson [fixture-id].webm"

    def downloader(source, output_dir):
        downloaded.write_bytes(b"downloaded media")
        info = downloaded.with_suffix(".info.json")
        info.write_text(json.dumps({"id": "fixture-id", "title": "真实课程标题"}), encoding="utf-8")
        return YtDlpDownload(media_path=downloaded, info_path=info)

    result = ingest_source(
        SourceInput.from_value("https://youtu.be/fixture-id"),
        workspace,
        downloader=downloader,
        prober=_probe,
    )

    assert result.manifest.kind == "youtube"
    assert result.manifest.video_id == "fixture-id"
    assert result.manifest.url == "https://www.youtube.com/watch?v=fixture-id"
    assert result.manifest.title == "真实课程标题"
    assert result.media_path.name == downloaded.name

    outside = tmp_path / "outside.webm"
    outside.write_bytes(b"outside")

    def escaping_downloader(source, output_dir):
        return YtDlpDownload(media_path=outside)

    with pytest.raises(WorkspacePathError):
        ingest_source(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            workspace,
            downloader=escaping_downloader,
            prober=_probe,
        )


def test_ingest_youtube_rejects_download_without_info_metadata(tmp_path: Path):
    workspace = Workspace.create(tmp_path / "runs", run_id="youtube-no-info")
    downloaded = workspace.media_dir / "fixture-id.webm"

    def downloader(source, output_dir):
        downloaded.write_bytes(b"downloaded media")
        return YtDlpDownload(media_path=downloaded)

    with pytest.raises(IngestError, match="info JSON|metadata"):
        ingest_source(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            workspace,
            downloader=downloader,
            prober=_probe,
        )


def test_ingest_youtube_rejects_info_metadata_for_a_different_video(tmp_path: Path):
    workspace = Workspace.create(tmp_path / "runs", run_id="youtube-wrong-id")
    downloaded = workspace.media_dir / "fixture-id.webm"

    def downloader(source, output_dir):
        downloaded.write_bytes(b"downloaded media")
        info = downloaded.with_suffix(".info.json")
        info.write_text(
            json.dumps({"id": "different-id", "title": "看似有效的标题"}),
            encoding="utf-8",
        )
        return YtDlpDownload(media_path=downloaded, info_path=info)

    with pytest.raises(IngestError, match="id"):
        ingest_source(
            SourceInput.from_value("https://youtu.be/fixture-id"),
            workspace,
            downloader=downloader,
            prober=_probe,
        )


def test_ingest_does_not_return_manifest_when_probe_fails(tmp_path: Path):
    original = tmp_path / "broken.mp4"
    original.write_bytes(b"broken")
    workspace = Workspace.create(tmp_path / "runs", run_id="broken")

    def failing_probe(path):
        raise IngestError("invalid media")

    with pytest.raises(IngestError, match="invalid media"):
        ingest_source(
            SourceInput.from_value(original),
            workspace,
            prober=failing_probe,
        )
    assert not list(workspace.metadata_dir.glob("*.json"))


def test_ingest_writes_manifest_atomically_under_lock_and_is_repeatable(tmp_path: Path):
    original = tmp_path / "repeatable.mp4"
    original.write_bytes(b"repeatable media")
    workspace = Workspace.create(tmp_path / "runs", run_id="repeatable")

    first = ingest_source(original, workspace, prober=_probe)
    manifest_path = workspace.metadata_dir / "source-manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == first.manifest.model_dump(mode="json")
    assert not list(workspace.metadata_dir.glob("*.tmp"))

    second = ingest_source(original, workspace, prober=_probe)
    assert second.manifest == first.manifest
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == second.manifest.model_dump(mode="json")


def test_ingest_preserves_published_manifest_when_rerun_probe_fails(tmp_path: Path):
    original = tmp_path / "locked.mp4"
    original.write_bytes(b"locked media")
    workspace = Workspace.create(tmp_path / "runs", run_id="locked-ingest")

    with workspace.write_lock():
        result = ingest_source(original, workspace, prober=_probe, lock=False)
    assert result.manifest.media_path.startswith("media/")

    manifest_path = workspace.metadata_dir / "source-manifest.json"
    assert manifest_path.is_file()
    previous_bytes = manifest_path.read_bytes()

    def fail_probe(path):
        raise IngestError("probe failed")

    with pytest.raises(IngestError, match="probe failed"):
        ingest_source(original, workspace, prober=fail_probe)
    assert manifest_path.read_bytes() == previous_bytes


def test_resolve_manifest_media_uses_reference_path_and_rechecks_hash(tmp_path: Path):
    original = tmp_path / "external reference.mp4"
    original.write_bytes(b"external media")
    workspace = Workspace.create(tmp_path / "runs", run_id="resolve")

    result = ingest_source(
        SourceInput.from_value(original, local_mode="reference"),
        workspace,
        prober=_probe,
    )
    assert resolve_manifest_media(result.manifest, workspace) == original.resolve()

    original.write_bytes(b"changed external media")
    with pytest.raises(IngestError, match="hash"):
        resolve_manifest_media(result.manifest, workspace)


def test_source_manifest_requires_reference_path_only_for_local_reference():
    base = {
        "schema_version": "1.0",
        "source_id": "src-test",
        "kind": "local",
        "title": "test",
        "media_path": "media/test.mp4",
        "sha256": "a" * 64,
        "duration_seconds": 1.0,
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264"}],
        "timebase": "1/90000",
    }
    with pytest.raises(ValueError, match="reference_path"):
        SourceManifest.model_validate({**base, "local_mode": "reference"})
    with pytest.raises(ValueError, match="reference_path"):
        SourceManifest.model_validate({**base, "local_mode": "copy", "reference_path": "/tmp/test.mp4"})
    with pytest.raises(ValueError, match="reference"):
        SourceManifest.model_validate(
            {
                **base,
                "kind": "youtube",
                "url": "https://www.youtube.com/watch?v=test-id",
                "video_id": "test-id",
                "reference_path": "/tmp/test.mp4",
            }
        )
