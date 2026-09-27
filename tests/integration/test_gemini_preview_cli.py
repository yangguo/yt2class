from __future__ import annotations

import json

from typer.testing import CliRunner

from yt2class import cli
from yt2class.gemini_video import GeminiPreviewError


def _preview(url: str, **_kwargs):
    return {
        "url": url,
        "model": "gemini-3.8-flash",
        "prompt": "test prompt",
        "text": "## 核心摘要\n内容。",
        "usage": {},
        "generated_at": "2026-09-27T00:00:00Z",
    }


def test_cli_accepts_one_url_and_saves_result(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "generate_video_preview", _preview)

    result = CliRunner().invoke(
        cli.app,
        ["gemini-preview", "--url", "https://youtu.be/first", "--output", str(tmp_path)],
    )

    assert result.exit_code == 0, result.output
    assert "OK" in result.output
    assert (tmp_path / "first.md").is_file()
    assert json.loads((tmp_path / "first.json").read_text(encoding="utf-8"))["url"] == (
        "https://www.youtube.com/watch?v=first"
    )


def test_cli_accepts_url_list_file_and_continues_after_one_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    links = tmp_path / "links.txt"
    links.write_text("https://youtu.be/first\nhttps://youtu.be/second\n", encoding="utf-8")

    def preview(url, **kwargs):
        if url.endswith("first"):
            raise GeminiPreviewError("Gemini HTTP 429: quota exceeded")
        return _preview(url, **kwargs)

    monkeypatch.setattr(cli, "generate_video_preview", preview)
    result = CliRunner().invoke(
        cli.app,
        ["gemini-preview", "--links", str(links), "--output", str(tmp_path / "out")],
    )

    assert result.exit_code == cli.EXIT_BATCH_PARTIAL
    assert "quota exceeded" in result.output
    assert (tmp_path / "out" / "second.md").is_file()
    assert not (tmp_path / "out" / "first.md").exists()


def test_cli_skips_invalid_link_but_processes_other_videos(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(cli, "generate_video_preview", _preview)
    links = tmp_path / "links.txt"
    links.write_text(
        "https://youtu.be/first\nhttps://example.com/invalid\nhttps://youtu.be/second\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        ["gemini-preview", "--links", str(links), "--output", str(tmp_path / "out")],
    )

    assert result.exit_code == cli.EXIT_BATCH_PARTIAL
    assert "https://example.com/invalid" in result.output
    assert (tmp_path / "out" / "first.md").is_file()
    assert (tmp_path / "out" / "second.md").is_file()


def test_cli_requires_gemini_key_without_sending_request(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    result = CliRunner().invoke(
        cli.app,
        ["gemini-preview", "--url", "https://youtu.be/first", "--output", str(tmp_path)],
    )
    assert result.exit_code == cli.EXIT_FAIL
    assert "GEMINI_API_KEY" in result.output
    assert not list(tmp_path.iterdir())


def test_cli_rejects_playlist_before_sending_request(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    result = CliRunner().invoke(
        cli.app,
        ["gemini-preview", "--url", "https://youtube.com/playlist?list=abc", "--output", str(tmp_path)],
    )
    assert result.exit_code == cli.EXIT_FAIL
    assert "playlist" in result.output
