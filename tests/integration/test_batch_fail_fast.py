from __future__ import annotations

from pathlib import Path

from yt2class.config import BuildSource
from yt2class.orchestration.batch import BatchItem, run_batch


def test_fail_fast_returns_report_without_cancelled_error(tmp_path: Path, monkeypatch):
    order = {"n": 0}

    def fake_execute(_output, build, **kwargs):
        order["n"] += 1
        if order["n"] == 1:
            raise RuntimeError("boom")
        return __import__(
            "yt2class.orchestration.pipeline", fromlist=["RunOutcome"]
        ).RunOutcome(manifest=None, workspace=None)  # type: ignore[arg-type]

    monkeypatch.setattr("yt2class.orchestration.batch.execute_run", fake_execute)
    items = [BatchItem(f"item-{index}", BuildSource(source_id=f"src-{index}")) for index in range(4)]
    report = run_batch(tmp_path, items, continue_on_error=False, max_workers=2)
    assert len(report.results) == 4
    assert not report.all_ok
    assert report.results[0].error == "boom"
    cancelled = [row for row in report.results if row.error == "cancelled"]
    assert cancelled
