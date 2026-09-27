from __future__ import annotations

import json

from typer.testing import CliRunner

from yt2class import cli


def test_cli_analyzes_links_and_writes_batch_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    links = tmp_path / "links.txt"
    links.write_text("https://youtu.be/one\nhttps://youtu.be/two\n", encoding="utf-8")

    def fake_preview(url, **kwargs):
        return {
            "url": url, "model": "test-model", "prompt": "test", "text": "分析",
            "usage": {}, "generated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(cli, "generate_video_preview", fake_preview)
    result = CliRunner().invoke(cli.app, [
        "gemini-preview", "--links", str(links), "--output", str(tmp_path / "out")
    ])

    assert result.exit_code == 0, result.output
    manifest = json.loads((tmp_path / "out" / "batch-manifest.json").read_text())
    assert [item["status"] for item in manifest["items"]] == ["success", "success"]
    assert (tmp_path / "out" / "one.md").is_file()
    assert (tmp_path / "out" / "two.md").is_file()


def test_cli_keeps_processing_after_one_failed_video(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def fake_preview(url, **kwargs):
        if url.endswith("one"):
            raise ValueError("quota exceeded")
        return {"url": url, "model": "test", "prompt": "test", "text": "分析", "usage": {}, "generated_at": "now"}

    monkeypatch.setattr(cli, "generate_video_preview", fake_preview)
    result = CliRunner().invoke(cli.app, [
        "gemini-preview", "--url", "https://youtu.be/one", "--url", "https://youtu.be/two",
        "--output", str(tmp_path / "out"),
    ])

    assert result.exit_code == cli.EXIT_BATCH_PARTIAL
    manifest = json.loads((tmp_path / "out" / "batch-manifest.json").read_text())
    assert [item["status"] for item in manifest["items"]] == ["failed", "success"]


def test_cli_keeps_valid_videos_after_an_invalid_input(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda **kwargs: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    links = tmp_path / "links.txt"
    links.write_text("not-a-youtube-url\nhttps://youtu.be/one\n", encoding="utf-8")

    def fake_preview(url, **kwargs):
        return {
            "url": url, "model": "test", "prompt": "test", "text": "完整分析",
            "usage": {}, "generated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(cli, "generate_video_preview", fake_preview)
    result = CliRunner().invoke(cli.app, [
        "gemini-preview", "--links", str(links), "--output", str(tmp_path / "out")
    ])

    assert result.exit_code == cli.EXIT_BATCH_PARTIAL
    manifest = json.loads((tmp_path / "out" / "batch-manifest.json").read_text())
    assert [item["status"] for item in manifest["items"]] == ["failed", "success"]
    assert (tmp_path / "out" / "one.md").read_text(encoding="utf-8") == "完整分析\n"


def test_cli_loads_api_key_from_cwd_dotenv(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=local-test-key\n", encoding="utf-8")
    seen = {}

    def fake_preview(url, **kwargs):
        seen["api_key"] = kwargs["api_key"]
        return {
            "url": url, "model": "test-model", "prompt": "test", "text": "分析",
            "usage": {}, "generated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(cli, "generate_video_preview", fake_preview)
    result = CliRunner().invoke(cli.app, [
        "gemini-preview", "--url", "https://youtu.be/one", "--output", str(tmp_path / "out")
    ])

    assert result.exit_code == 0, result.output
    assert seen["api_key"] == "local-test-key"
    assert "local-test-key" not in result.output


def test_cli_uses_gemini_model_from_dotenv_and_cli_can_override(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    (tmp_path / ".env").write_text(
        "GEMINI_API_KEY=local-test-key\nGEMINI_MODEL=gemini-env-model\n",
        encoding="utf-8",
    )
    seen = []

    def fake_preview(url, **kwargs):
        seen.append(kwargs["model"])
        return {
            "url": url, "model": kwargs["model"], "prompt": "test", "text": "分析",
            "usage": {}, "generated_at": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr(cli, "generate_video_preview", fake_preview)
    runner = CliRunner()
    common = ["--url", "https://youtu.be/one"]

    from_env = runner.invoke(cli.app, ["gemini-preview", *common, "--output", str(tmp_path / "env-out")])
    override = runner.invoke(
        cli.app,
        ["gemini-preview", *common, "--model", "gemini-cli-model", "--output", str(tmp_path / "cli-out")],
    )

    assert from_env.exit_code == 0, from_env.output
    assert override.exit_code == 0, override.output
    assert seen == ["gemini-env-model", "gemini-cli-model"]


def test_cli_loads_legacy_image_model_settings_from_dotenv(tmp_path, monkeypatch):
    from yt2class.llm import ModelConfig

    monkeypatch.chdir(tmp_path)
    for key in (
        "YT2CLASS_MODEL_URL", "YT2CLASS_MODEL_KEY", "YT2CLASS_MODEL",
        "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "YT2CLASS_MODEL_URL=https://vision.example/v1/chat/completions\n"
        "YT2CLASS_MODEL_KEY=local-vision-key\n"
        "YT2CLASS_MODEL=vision-env-model\n",
        encoding="utf-8",
    )
    links = tmp_path / "links.txt"
    links.write_text("https://youtu.be/one\n", encoding="utf-8")
    seen = {}

    def fake_build_batch(urls, output, **kwargs):
        seen["urls"] = urls
        seen["model"] = ModelConfig.from_environment().model
        return []

    monkeypatch.setattr(cli, "build_batch", fake_build_batch)
    result = CliRunner().invoke(cli.app, ["build", "--links", str(links), "--output", str(tmp_path / "out")])

    assert result.exit_code == 0, result.output
    assert seen == {"urls": ["https://youtu.be/one"], "model": "vision-env-model"}
