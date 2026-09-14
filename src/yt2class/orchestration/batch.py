"""Batch driver with per-item isolation, concurrency cap, and partial failure reporting."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event
import threading

from yt2class.config import BuildSource, CourseConfig
from yt2class.orchestration.manifest_io import new_run_id
from yt2class.orchestration.pipeline import PipelineError, PipelinePaused, RunOutcome, execute_run

MAX_BATCH_CONCURRENCY = 2


@dataclass(frozen=True)
class BatchItem:
    label: str
    build: BuildSource


@dataclass
class BatchItemResult:
    label: str
    ok: bool
    run_id: str | None = None
    error: str | None = None
    outcome: RunOutcome | None = None


@dataclass
class BatchReport:
    results: list[BatchItemResult]

    @property
    def all_ok(self) -> bool:
        return all(item.ok for item in self.results)

    @property
    def any_ok(self) -> bool:
        return any(item.ok for item in self.results)


def run_batch(
    output_dir: Path,
    items: list[BatchItem],
    *,
    config: CourseConfig | None = None,
    continue_on_error: bool = True,
    max_workers: int = MAX_BATCH_CONCURRENCY,
    cancel_event: Event | None = None,
) -> BatchReport:
    if max_workers < 1:
        raise ValueError("max_workers must be at least 1")
    if max_workers > MAX_BATCH_CONCURRENCY:
        max_workers = MAX_BATCH_CONCURRENCY

    shared_cancel = cancel_event or Event()
    results: list[BatchItemResult] = []
    lock = threading.Lock()

    def _run_one(item: BatchItem) -> BatchItemResult:
        if shared_cancel.is_set():
            return BatchItemResult(label=item.label, ok=False, error="cancelled")
        run_id = item.build.run_id or new_run_id()
        build = BuildSource(
            url=item.build.url,
            video=item.build.video,
            subtitles=item.build.subtitles,
            run_id=run_id,
            source_id=item.build.source_id,
        )
        try:
            outcome = execute_run(
                output_dir,
                build=build,
                config=config,
                run_id=run_id,
            )
            return BatchItemResult(label=item.label, ok=True, run_id=run_id, outcome=outcome)
        except PipelinePaused as error:
            return BatchItemResult(label=item.label, ok=False, run_id=run_id, error=str(error))
        except PipelineError as error:
            return BatchItemResult(label=item.label, ok=False, run_id=run_id, error=str(error))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run_one, item): item for item in items}
        for future in as_completed(futures):
            result = future.result()
            with lock:
                results.append(result)
            if not result.ok and not continue_on_error:
                shared_cancel.set()
                for pending in futures:
                    pending.cancel()

    # Preserve input order for stable reports.
    order = {item.label: index for index, item in enumerate(items)}
    results.sort(key=lambda row: order.get(row.label, 0))
    return BatchReport(results=results)


__all__ = [
    "BatchItem",
    "BatchItemResult",
    "BatchReport",
    "MAX_BATCH_CONCURRENCY",
    "run_batch",
]
