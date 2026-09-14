from __future__ import annotations

from pathlib import Path

from yt2class.orchestration.cache import atomic_write_text, file_sha256, stage_cache_hit, mark_stage_complete


def test_atomic_write_and_stage_marker(tmp_path: Path):
    target = tmp_path / "artifact.json"
    first = atomic_write_text(target, '{"ok": true}\n')
    assert target.is_file()
    assert first.sha256 == file_sha256(target)
    atomic_write_text(target, '{"ok": false}\n')
    assert file_sha256(target) != first.sha256
    mark_stage_complete(tmp_path, "outline", "key-1")
    assert stage_cache_hit(tmp_path, "outline", "key-1")
