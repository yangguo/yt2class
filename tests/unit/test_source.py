from __future__ import annotations

import multiprocessing
from pathlib import Path
import time

import pytest

from yt2class.domain.source import (
    SourceInput,
    SourceInputError,
    content_sha256,
    read_source_inputs,
)
from yt2class.orchestration.workspace import Workspace, WorkspaceBusy, WorkspacePathError


def test_youtube_input_normalizes_tracking_parameters_and_extracts_video_id():
    source = SourceInput.from_value(
        "  https://youtu.be/AbC_123-xYz?t=42&si=tracking-value  "
    )

    assert source.kind == "youtube"
    assert source.video_id == "AbC_123-xYz"
    assert source.normalized_value == "https://www.youtube.com/watch?v=AbC_123-xYz"
    assert source.source_key == "youtube:AbC_123-xYz"


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=abc&list=playlist-id",
        "https://www.youtube.com/playlist?list=playlist-id",
        "https://example.test/video",
    ],
)
def test_youtube_playlist_and_non_youtube_urls_are_rejected(value: str):
    with pytest.raises(SourceInputError):
        SourceInput.from_value(value)


@pytest.mark.parametrize("suffix", [".mp4", ".MKV", ".webm", ".Mov"])
def test_local_media_extensions_and_content_identity_are_supported(tmp_path: Path, suffix: str):
    first = tmp_path / f"课程 sample{suffix}"
    second = tmp_path / f"另一个路径{suffix}"
    first.write_bytes(b"same media bytes")
    second.write_bytes(b"same media bytes")

    left = SourceInput.from_value(first, local_mode="copy")
    right = SourceInput.from_value(second, local_mode="reference")

    assert left.kind == right.kind == "local"
    assert left.local_mode == "copy"
    assert right.local_mode == "reference"
    assert content_sha256(first) == content_sha256(second)
    assert left.content_sha256 == right.content_sha256


def test_local_empty_and_unsupported_files_are_rejected(tmp_path: Path):
    empty = tmp_path / "empty.mp4"
    empty.touch()
    with pytest.raises(SourceInputError, match="empty"):
        SourceInput.from_value(empty)

    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("not media", encoding="utf-8")
    with pytest.raises(SourceInputError, match="container"):
        SourceInput.from_value(unsupported)


def test_local_content_identity_changes_when_source_bytes_change(tmp_path: Path):
    media = tmp_path / "mutable.mp4"
    media.write_bytes(b"version one")
    source = SourceInput.from_value(media)
    first_hash = source.content_sha256

    media.write_bytes(b"version two")

    assert source.content_sha256 != first_hash


def test_batch_source_list_preserves_order_and_skips_comments(tmp_path: Path):
    media = tmp_path / "sample video.mp4"
    media.write_bytes(b"video")
    manifest = tmp_path / "sources.txt"
    manifest.write_text(
        "# lecture sources\nhttps://youtu.be/first-id\n"
        f"{media}\n\nhttps://youtu.be/second-id\n",
        encoding="utf-8",
    )

    sources = read_source_inputs(manifest)

    assert [source.kind for source in sources] == ["youtube", "local", "youtube"]
    assert [source.source_key for source in sources] == [
        "youtube:first-id",
        f"local:{media.resolve()}",
        "youtube:second-id",
    ]


def test_workspace_resolves_run_root_symlink_and_confines_artifacts(tmp_path: Path):
    real_root = tmp_path / "real-output"
    real_root.mkdir()
    alias_root = tmp_path / "alias-output"
    alias_root.symlink_to(real_root, target_is_directory=True)

    workspace = Workspace.create(alias_root, run_id="lesson-1")
    safe_file = workspace.safe_path("media/课程 sample.mp4", create_parent=True)
    safe_file.write_bytes(b"ok")

    assert workspace.root == (real_root / "lesson-1").resolve()
    assert safe_file.is_relative_to(workspace.root)

    outside = tmp_path / "outside"
    outside.mkdir()
    escape = workspace.root / "escape"
    escape.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspacePathError):
        workspace.safe_path("escape/file.txt", create_parent=True)


def test_workspace_has_single_run_write_lock(tmp_path: Path):
    workspace = Workspace.create(tmp_path, run_id="locked")

    with workspace.write_lock():
        with pytest.raises(WorkspaceBusy):
            with workspace.write_lock():
                pass


def test_stale_write_lock_from_dead_pid_is_reclaimed(tmp_path: Path):
    workspace = Workspace.create(tmp_path, run_id="stale-lock")
    workspace.lock_path.write_text("pid=999999\n", encoding="ascii")
    with workspace.write_lock():
        assert workspace.lock_path.is_file()
    assert workspace.lock_path.is_file()


def _stale_lock_contest(root: str, run_id: str, result_path: str, go_path: str) -> None:
    from yt2class.orchestration.workspace import Workspace, WorkspaceBusy

    workspace = Workspace.create(Path(root), run_id=run_id)
    while not Path(go_path).exists():
        time.sleep(0.01)
    try:
        with workspace.write_lock():
            Path(result_path).write_text("held", encoding="ascii")
            time.sleep(0.4)
    except WorkspaceBusy:
        Path(result_path).write_text("busy", encoding="ascii")


def test_two_stale_lock_reclaimers_cannot_both_own_the_workspace(tmp_path: Path):
    workspace = Workspace.create(tmp_path, run_id="stale-race")
    workspace.lock_path.write_text("pid=999999\n", encoding="ascii")
    go_path = tmp_path / "go"
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    ctx = multiprocessing.get_context("spawn")
    processes = [
        ctx.Process(
            target=_stale_lock_contest,
            args=(str(tmp_path), "stale-race", str(path), str(go_path)),
        )
        for path in (first, second)
    ]
    for process in processes:
        process.start()
    time.sleep(0.15)
    go_path.write_text("go", encoding="ascii")
    for process in processes:
        process.join(timeout=5.0)
        assert process.exitcode == 0
    assert {first.read_text(encoding="ascii"), second.read_text(encoding="ascii")} == {
        "held",
        "busy",
    }


def _lock_handoff_holder(root: str, run_id: str, ready_path: str, release_path: str) -> None:
    from yt2class.orchestration.workspace import Workspace

    workspace = Workspace.create(Path(root), run_id=run_id)
    with workspace.write_lock():
        Path(ready_path).write_text("ready", encoding="ascii")
        while not Path(release_path).exists():
            time.sleep(0.01)


def test_lock_handoff_cannot_produce_dual_owners(tmp_path: Path):
    workspace = Workspace.create(tmp_path, run_id="handoff")
    ready = tmp_path / "holder-ready"
    release = tmp_path / "holder-release"
    ctx = multiprocessing.get_context("spawn")
    holder = ctx.Process(
        target=_lock_handoff_holder,
        args=(str(tmp_path), "handoff", str(ready), str(release)),
    )
    holder.start()
    deadline = time.time() + 5
    while not ready.exists() and time.time() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    inode = workspace.lock_path.stat().st_ino
    with pytest.raises(WorkspaceBusy):
        with workspace.write_lock():
            pass
    release.write_text("go", encoding="ascii")
    holder.join(timeout=5.0)
    assert holder.exitcode == 0
    assert workspace.lock_path.is_file()
    assert workspace.lock_path.stat().st_ino == inode
    with workspace.write_lock():
        assert workspace.lock_path.stat().st_ino == inode
        with pytest.raises(WorkspaceBusy):
            with workspace.write_lock():
                pass
    assert workspace.lock_path.is_file()
    assert workspace.lock_path.stat().st_ino == inode
