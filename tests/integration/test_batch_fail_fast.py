from __future__ import annotations

from pathlib import Path
from threading import Event

from yt2class.config import BuildSource
from yt2class.orchestration.batch import BatchItem, run_batch


def test_fail_fast_returns_report_without_cancelled_error(tmp_path: Path, monkeypatch):
    nonfailing_started = Event()

    def fake_execute(_output, build, **kwargs):
        if build.source_id == "src-0":
            assert nonfailing_started.wait(timeout=1.0)
            raise RuntimeError("boom")
        nonfailing_started.set()
        assert kwargs["cancel_event"].wait(timeout=1.0)
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
