"""M7 fault-injection matrix: resume / fail-closed behaviors on fixtures."""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from yt2class.adapters.asr import ASRCancelled, ASRRequest, run_asr
from yt2class.adapters.ytdlp import YtDlpCancelled, download_video
from yt2class.config import BuildSource, CourseConfig
from yt2class.domain.source import SourceInput
from yt2class.orchestration import pipeline as orch_pipeline
from yt2class.orchestration.budget import BudgetExceeded, BudgetLimits, RunBudget
from yt2class.orchestration.cache import CacheCorrupt
from yt2class.orchestration.pipeline import execute_run
from yt2class.stages.review import ReviewEdits, StaleReviewError, apply_review_edits, build_review_bundle
from tests.helpers.m5 import build_evidence_bundle
from tests.integration.test_review_roundtrip import DIGEST_A, _verified_bundle, grounding_provider


def test_download_cancel_before_start_fail_closed(tmp_path: Path):
    source = SourceInput.from_value("https://www.youtube.com/watch?v=fixture")
    cancel = Event()
    cancel.set()
    with pytest.raises(YtDlpCancelled):
        download_video(source, tmp_path, cancel_event=cancel)


def test_asr_cancel_before_start_fail_closed(tmp_path: Path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"synthetic audio")
    request = ASRRequest.model_validate(
        {
            "request_id": "asr-cancel",
            "source_id": "src-asr",
            "audio_path": audio,
            "language": "ja",
            "engine": "whisperx",
            "model": "tiny",
            "device": "cpu",
            "align": True,
            "diarize": False,
            "offset_seconds": 0.0,
        }
    )
    cancel = Event()
    cancel.set()

    def runner(_cmd, **_kwargs):
        raise AssertionError("runner should not be invoked when cancelled")

    with pytest.raises(ASRCancelled):
        run_asr(request, runner=runner, cancel_event=cancel)


def test_provider_budget_429_class_fail_closed():
    budget = RunBudget(limits=BudgetLimits(max_model_calls=1))
    budget.charge(model_calls=1)
    with pytest.raises(BudgetExceeded):
        budget.charge(model_calls=1)


def test_analysis_half_done_resume_skips_rerun(tmp_path: Path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    calls = {"analysis": 0}
    real = orch_pipeline.analyze_evidence_bundle

    def counting(*args, **kwargs):
        calls["analysis"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "analyze_evidence_bundle", counting)
    build = BuildSource(source_id=bundle.source_id)
    cfg = CourseConfig()
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        stop_after="reduce_knowledge",
    )
    execute_run(
        tmp_path,
        build=build,
        config=cfg,
        run_id=workspace.root.name,
        evidence_bundle=bundle,
        resume=True,
        stop_after="reduce_knowledge",
    )
    assert calls["analysis"] == 1


def test_stale_review_revision_fail_closed():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    page_id = bundle.plan.pages[0].id
    with pytest.raises(StaleReviewError, match="revision"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision + 1,
                baseline_hashes=bundle.baseline_hashes,
                ops=[{"op": "lock", "page_id": page_id}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )


def test_render_truncated_pptx_fail_closed(tmp_path: Path):
    pptx = tmp_path / "lesson.pptx"
    pptx.write_bytes(b"x")

    def _validate_render(_: Path) -> None:
        if not pptx.is_file() or pptx.stat().st_size < 100:
            raise CacheCorrupt("pptx missing or truncated")

    with pytest.raises(CacheCorrupt, match="truncated"):
        _validate_render(pptx)


def test_stale_review_baseline_hash_fail_closed():
    outcome, transcript, visual, topics = _verified_bundle()
    bundle = build_review_bundle(
        knowledge=outcome.knowledge,
        plan=outcome.plan,
        report=outcome.report,
        transcript=transcript,
        visual=visual,
        course_map=topics,
    )
    stale_hashes = dict(bundle.baseline_hashes)
    stale_hashes["knowledge"] = DIGEST_A
    with pytest.raises(StaleReviewError, match="hash"):
        apply_review_edits(
            bundle,
            ReviewEdits(
                revision=bundle.revision,
                baseline_hashes=stale_hashes,
                ops=[{"op": "lock", "page_id": bundle.plan.pages[0].id}],
            ),
            knowledge=outcome.knowledge,
            transcript=transcript,
            visual=visual,
            provider=grounding_provider(),
        )
