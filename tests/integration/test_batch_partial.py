from __future__ import annotations

from pathlib import Path

import pytest

from yt2class.config import BuildSource
from yt2class.orchestration.batch import BatchItem, run_batch
from yt2class.orchestration.pipeline import RunOutcome


def test_batch_continues_after_unexpected_error(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def fake_execute(_output, build, **kwargs):
        calls["n"] += 1
        if build.source_id == "fail":
            raise RuntimeError("boom")
        return RunOutcome(manifest=None, workspace=None)  # type: ignore[arg-type]

    monkeypatch.setattr("yt2class.orchestration.batch.execute_run", fake_execute)
    report = run_batch(
        tmp_path,
        [
            BatchItem("ok", BuildSource(source_id="ok")),
            BatchItem("bad", BuildSource(source_id="fail")),
        ],
        continue_on_error=True,
    )
    assert calls["n"] == 2
    assert report.any_ok and not report.all_ok
    assert report.results[1].error == "boom"
