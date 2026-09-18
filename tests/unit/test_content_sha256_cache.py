from __future__ import annotations

from yt2class.domain.source import clear_content_sha256_cache, content_sha256


def test_content_sha256_reuses_digest_for_unchanged_file(tmp_path):
    path = tmp_path / "clip.bin"
    path.write_bytes(b"same bytes")
    clear_content_sha256_cache()
    first = content_sha256(path)
    second = content_sha256(path)
    assert first == second
