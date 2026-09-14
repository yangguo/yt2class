from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from yt2class import cli


def test_doctor_emits_structured_checks():
    runner = CliRunner()
    result = runner.invoke(cli.app, ["doctor", "--json"], prog_name="yt2class")
    assert result.exit_code in {0, 1}
    payload = json.loads(result.stdout)
    assert "checks" in payload
    assert any(item["name"] == "node" for item in payload["checks"])


def test_resume_requires_run_dir(tmp_path: Path):
    runner = CliRunner()
    missing = tmp_path / "nope"
    result = runner.invoke(
        cli.app,
        ["resume", "--run", str(missing)],
        prog_name="yt2class",
    )
    assert result.exit_code == 1


def test_batch_continue_on_error_reports_partial_failure(tmp_path: Path, monkeypatch):
    from yt2class.orchestration.batch import BatchItemResult, BatchReport

    def fake_batch(*_args, **_kwargs):
        return BatchReport(
            results=[
                BatchItemResult(label="item-a", ok=True, run_id="run-a"),
                BatchItemResult(label="item-b", ok=False, run_id="run-b", error="boom"),
            ]
        )

    monkeypatch.setattr(cli, "run_product_batch", fake_batch)
    manifest_file = tmp_path / "batch.txt"
    manifest_file.write_text("item-a\nitem-b\n", encoding="utf-8")
    result = CliRunner().invoke(
        cli.app,
        ["batch", "--inputs", str(manifest_file), "--output", str(tmp_path / "runs")],
        prog_name="yt2class",
    )
    assert result.exit_code == 3
    assert "item-b" in (result.stdout + result.stderr)
