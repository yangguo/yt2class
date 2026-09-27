from __future__ import annotations

import json
from pathlib import Path

import pytest
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


@pytest.mark.parametrize("command", ["build-run", "resume"])
def test_bind_failure_is_reported_with_nonzero_exit(tmp_path, monkeypatch, command):
    from yt2class.domain.editorial import PageIntent
    from yt2class.stages.bind_spec import _bind_summary

    def fail_bind(*args, **kwargs):
        _bind_summary(PageIntent(id="empty-summary", type="summary", title="Summary",
                      claim_ids=[], selection_reason="regression", quality_label="draft"), claims={})

    monkeypatch.setattr(cli, "execute_run", fail_bind)
    if command == "resume":
        from yt2class.orchestration.manifest_io import initial_manifest, save_manifest
        save_manifest(tmp_path, initial_manifest(run_id="run-test", source_id="src-test"))
        args = ["resume", "--run", str(tmp_path)]
        message = "Resume failed:"
    else:
        args = ["build-run", "--url", "https://youtu.be/fixture-id", "--output", str(tmp_path)]
        message = "Run failed:"
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 1
    assert message in result.output
    assert "OK " not in result.output



def test_build_run_validation_failure_is_reported(tmp_path, monkeypatch):
    from yt2class.domain.slide_spec_v3 import SlidePage

    def fail_validation(*args, **kwargs):
        SlidePage(id="invalid-summary", type="summary", title="Summary", claim_ids=[])

    monkeypatch.setattr(cli, "execute_run", fail_validation)
    result = CliRunner().invoke(cli.app, ["build-run", "--url", "https://youtu.be/fixture-id",
                                          "--output", str(tmp_path)])
    assert result.exit_code == 1
    assert "Run failed:" in result.output


def test_build_run_pipeline_failure_is_reported(tmp_path, monkeypatch):
    def fail_pipeline(*args, **kwargs):
        raise cli.PipelineError("fixture pipeline failure")

    monkeypatch.setattr(cli, "execute_run", fail_pipeline)
    result = CliRunner().invoke(cli.app, ["build-run", "--url", "https://youtu.be/fixture-id",
                                          "--output", str(tmp_path)])
    assert result.exit_code == 1
    assert "Run failed: fixture pipeline failure" in result.output


def test_plan_cli_writes_content_coverage_with_source_evidence_ids(tmp_path: Path, monkeypatch):
    from yt2class.domain.editorial import EditorialPlan, PageIntent
    from tests.helpers.m3 import lecture_knowledge

    knowledge, _topics, transcript, visual = lecture_knowledge()
    claim = knowledge.iter_claims()[0]
    plan = EditorialPlan(
        schema_version="1.0",
        source_id=knowledge.source_id,
        target_pages=4,
        max_pages=4,
        pages=[
            PageIntent(
                id="page-content",
                type="content",
                title="Fixture lesson",
                claim_ids=[claim.id],
                selection_reason="fixture",
            )
        ],
    )
    monkeypatch.setattr(cli, "plan_deck", lambda *_args, **_kwargs: plan)

    paths = {
        "knowledge": tmp_path / "knowledge.json",
        "transcript": tmp_path / "transcript.json",
        "visual": tmp_path / "visual.json",
    }
    for key, document in (
        ("knowledge", knowledge),
        ("transcript", transcript),
        ("visual", visual),
    ):
        paths[key].write_text(document.model_dump_json(indent=2), encoding="utf-8")
    output = tmp_path / "editorial"

    result = CliRunner().invoke(
        cli.app,
        [
            "plan",
            "--knowledge", str(paths["knowledge"]),
            "--transcript", str(paths["transcript"]),
            "--visual", str(paths["visual"]),
            "--output", str(output),
        ],
        prog_name="yt2class",
    )

    assert result.exit_code == 0, result.output
    report_path = output / "content-coverage.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    covered = next(item for item in report["claims"] if item["claim_id"] == claim.id)
    assert report["evidence_validation"]["status"] == "checked"
    assert covered["body_page_ids"] == ["page-content"]
    assert covered["invalid_evidence_ids"] == []
    assert "verification_report_sha256" in report["digests"]
