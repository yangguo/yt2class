"""M7 recoverability: incremental coverage on production APIs (not a duplicate fault matrix)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from yt2class.adapters.providers.base import ModelRequest, ModelResult, Provider, Usage
from yt2class.adapters.render.base import RenderError
from yt2class.adapters.render.pptxgenjs import validate_pptx_package
from yt2class.config import BuildSource, CourseConfig
from yt2class.domain.slide_spec_v3 import SlideSpecV3
from yt2class.orchestration import pipeline as orch_pipeline
from yt2class.orchestration.budget import BudgetLimits, RunBudget
from yt2class.orchestration.cache import atomic_write_text, mark_stage_complete, stage_artifact_path
from yt2class.orchestration.cache_policy import validated_cache_hit
from yt2class.orchestration.manifest_io import load_manifest, stage_record
from yt2class.orchestration.pipeline import PipelineError, execute_run
from yt2class.orchestration.provider_gate import wrap_provider
from yt2class.orchestration.retry import RetryPolicy, RetryableError, call_with_retry
from tests.helpers.m2 import frames_caps
from tests.helpers.m5 import build_evidence_bundle

FIXTURE_SPEC = (
    Path(__file__).resolve().parents[1] / "fixtures/contracts/slide_spec_v3.valid.json"
)


def test_execute_run_rejects_non_fake_provider(tmp_path: Path):
    workspace, bundle = build_evidence_bundle(tmp_path)
    cfg = CourseConfig().model_copy(
        update={"analysis": CourseConfig().analysis.model_copy(update={"provider": "openai"})}
    )
    with pytest.raises(PipelineError, match="unsupported analysis provider"):
        execute_run(
            tmp_path,
            build=BuildSource(source_id=bundle.source_id),
            config=cfg,
            run_id=workspace.root.name,
            evidence_bundle=bundle,
            stop_after="reduce_knowledge",
        )


def test_budgeted_provider_retries_http_429_through_call_with_retry(monkeypatch):
    """Production wrap_provider path uses call_with_retry when retry_policy is set."""

    class FlakyProvider(Provider):
        def __init__(self) -> None:
            super().__init__(frames_caps())
            self.attempts = 0

        def _complete(self, request: ModelRequest, *, cancel_event=None) -> ModelResult:
            self.attempts += 1
            if self.attempts < 3:
                raise RetryableError("rate limited", status_code=429, retry_after=0.001)
            return ModelResult(
                request_id=request.request_id,
                structured={"ok": True},
                usage=Usage(
                    request_id=request.request_id,
                    input_tokens=10,
                    output_tokens=5,
                ),
            )

    monkeypatch.setattr("yt2class.orchestration.retry.random.random", lambda: 0.0)
    inner = FlakyProvider()
    budget = RunBudget(limits=BudgetLimits(max_model_calls=10))
    wrapped = wrap_provider(
        inner,
        budget,
        retry_policy=RetryPolicy(max_attempts=3, jitter_ratio=0.0),
    )
    request = ModelRequest(
        request_id="req-429",
        role="segment",
        modalities=["text"],
        estimated_input_tokens=100,
        estimated_output_tokens=50,
        payload_digest="d" * 64,
    )
    result = wrapped.complete(request)
    assert result.structured == {"ok": True}
    assert inner.attempts == 3


def test_call_with_retry_surfaces_429_after_max_attempts():
    policy = RetryPolicy(max_attempts=2, jitter_ratio=0.0)

    def always_429() -> None:
        raise RetryableError("rate limited", status_code=429, retry_after=0.001)

    with pytest.raises(RetryableError, match="rate limited"):
        call_with_retry(always_429, policy=policy, sleep=lambda _s: None)


def test_corrupt_segment_manifest_forces_analysis_rerun(tmp_path: Path, monkeypatch):
    """Partial analysis artifact corruption on resume (see also test_resume.test_corrupt_cache_forces_rerun)."""

    workspace, bundle = build_evidence_bundle(tmp_path)
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
    manifest = load_manifest(workspace.root)
    assert manifest is not None
    key = stage_record(manifest, "analyze_segments").cache_key
    assert key is not None
    corrupt = stage_artifact_path(workspace.root, "analyze_segments", key, "segment-manifest.json")
    atomic_write_text(corrupt, "{not-json")

    calls = {"analysis": 0}
    real = orch_pipeline.analyze_evidence_bundle

    def counting(*args, **kwargs):
        calls["analysis"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(orch_pipeline, "analyze_evidence_bundle", counting)
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


def test_validate_pptx_package_rejects_truncated_bytes(tmp_path: Path):
    spec = SlideSpecV3.model_validate(json.loads(FIXTURE_SPEC.read_text(encoding="utf-8")))
    pptx = tmp_path / "delivery" / "lesson.pptx"
    pptx.parent.mkdir(parents=True, exist_ok=True)
    pptx.write_bytes(b"truncated")
    with pytest.raises(RenderError, match="PPTX ZIP"):
        validate_pptx_package(pptx, spec=spec)


def test_render_cache_validation_invalidates_truncated_pptx_marker(tmp_path: Path):
    """Same validator stack as pipeline run_delivery_stages (_validate_render → validate_pptx_package)."""

    spec = SlideSpecV3.model_validate(json.loads(FIXTURE_SPEC.read_text(encoding="utf-8")))
    pptx = tmp_path / "delivery" / "lesson.pptx"
    pptx.parent.mkdir(parents=True, exist_ok=True)
    pptx.write_bytes(b"x" * 50)
    render_key = "render-m7-key"
    mark_stage_complete(tmp_path, "render", render_key)
    marker = tmp_path / "cache" / "render" / render_key / ".complete"
    assert marker.is_file()

    def _validate_render(_: Path) -> None:
        if not pptx.is_file() or pptx.stat().st_size < 100:
            from yt2class.orchestration.cache import CacheCorrupt

            raise CacheCorrupt("pptx missing or truncated")
        validate_pptx_package(pptx, spec=spec)

    assert not validated_cache_hit(tmp_path, "render", render_key, _validate_render)
    assert not marker.is_file()
