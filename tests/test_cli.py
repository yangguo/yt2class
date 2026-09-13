from typer.testing import CliRunner

from yt2class import cli


def test_help_exposes_build_subcommand():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["--help"], prog_name="yt2class")

    assert result.exit_code == 0
    assert "COMMAND" in result.stdout
    assert "build" in result.stdout.lower()
    assert "analyze" in result.stdout.lower()

    build_help = runner.invoke(cli.app, ["build", "--help"], prog_name="yt2class")
    assert build_help.exit_code == 0
    assert "Usage: yt2class build" in build_help.stdout
    analyze_help = runner.invoke(cli.app, ["analyze", "--help"], prog_name="yt2class")
    assert analyze_help.exit_code == 0
    assert "FakeProvider" in analyze_help.stdout or "fake" in analyze_help.stdout.lower()


def test_build_command_passes_batch_options_to_pipeline(tmp_path, monkeypatch):
    assert hasattr(cli, "build_batch")

    links = tmp_path / "links.txt"
    links.write_text("https://youtu.be/test\n", encoding="utf-8")
    calls = {}

    class Result:
        url = "https://youtu.be/test"
        pptx_path = tmp_path / "lesson.pptx"
        candidate_count = 3
        selected_count = 1

    def fake_read_urls(path):
        calls["urls_path"] = path
        return ["https://youtu.be/test"]

    def fake_build_batch(urls, output_dir, **kwargs):
        calls.update(urls=urls, output_dir=output_dir, kwargs=kwargs)
        return [Result()]

    monkeypatch.setattr(cli, "read_urls", fake_read_urls)
    monkeypatch.setattr(cli, "build_batch", fake_build_batch)
    result = CliRunner().invoke(
        cli.app,
        ["build", "--links", str(links), "--output", str(tmp_path / "out"), "--max-slides", "4"],
        prog_name="yt2class",
    )

    assert result.exit_code == 0, result.stdout
    assert calls["urls_path"] == links
    assert calls["urls"] == ["https://youtu.be/test"]
    assert calls["kwargs"]["max_slides"] == 4
