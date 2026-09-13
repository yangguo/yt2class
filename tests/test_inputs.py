from pathlib import Path

import pytest

from yt2class import inputs


def test_read_urls_ignores_blank_lines_and_comments(tmp_path: Path):
    source = tmp_path / "links.txt"
    source.write_text(
        "# N5 grammar\nhttps://youtu.be/a\n\nhttps://youtube.com/watch?v=b\n",
        encoding="utf-8",
    )

    assert hasattr(inputs, "read_urls")
    assert inputs.read_urls(source) == [
        "https://youtu.be/a",
        "https://youtube.com/watch?v=b",
    ]


def test_lesson_id_is_stable_and_safe():
    url = "https://youtube.com/watch?v=abc"

    assert hasattr(inputs, "lesson_id")
    assert inputs.lesson_id(url) == inputs.lesson_id(url)
    assert "/" not in inputs.lesson_id(url)


def test_create_run_paths_stays_inside_requested_output_root(tmp_path: Path):
    assert hasattr(inputs, "create_run_paths")

    paths = inputs.create_run_paths(tmp_path, "https://youtu.be/a")

    assert paths.root.is_relative_to(tmp_path.resolve())
    assert paths.media_dir.is_dir()
    assert paths.frames_dir.is_dir()


def test_read_urls_rejects_non_youtube_url(tmp_path: Path):
    source = tmp_path / "links.txt"
    source.write_text("https://example.test/video\n", encoding="utf-8")

    with pytest.raises(ValueError, match="YouTube"):
        inputs.read_urls(source)
