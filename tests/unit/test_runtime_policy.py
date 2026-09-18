from __future__ import annotations

import pytest

from yt2class.orchestration.budget import BudgetExceeded, BudgetLimits, RunBudget
from yt2class.orchestration.retry import (
    NonRetryableError,
    RetryPolicy,
    RetryableError,
    call_with_retry,
    classify_http_status,
    parse_retry_after,
)


def test_budget_blocks_when_exceeded():
    budget = RunBudget(limits=BudgetLimits(max_model_calls=1, max_estimated_usd=0.01))
    budget.charge(estimated_usd=0.005)
    with pytest.raises(BudgetExceeded):
        budget.charge(estimated_usd=0.01)


def test_budget_does_not_pause_on_temporary_actual_plus_reservations():
    budget = RunBudget(limits=BudgetLimits(max_input_tokens=10))
    first = budget.reserve(input_tokens=5)
    second = budget.reserve(input_tokens=5)

    budget.settle(
        first,
        input_tokens=8,
        output_tokens=0,
        image_count=0,
        video_seconds=0.0,
    )
    assert budget.consumed.input_tokens == 8
    assert budget.paused is False

    budget.settle(
        second,
        input_tokens=2,
        output_tokens=0,
        image_count=0,
        video_seconds=0.0,
    )
    assert budget.consumed.input_tokens == 10
    assert budget.paused is False


def test_retry_honors_retry_after_and_jitter(monkeypatch):
    policy = RetryPolicy(max_attempts=3, base_delay_seconds=1.0, jitter_ratio=0.0)
    sleeps: list[float] = []
    attempts = {"n": 0}

    def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RetryableError("rate limited", status_code=429, retry_after=0.5)
        return "ok"

    monkeypatch.setattr("yt2class.orchestration.retry.random.random", lambda: 0.0)
    result = call_with_retry(flaky, policy=policy, sleep=sleeps.append)
    assert result == "ok"
    assert sleeps == [0.5, 0.5]


def test_retry_after_not_capped_by_max_delay():
    policy = RetryPolicy(max_delay_seconds=8.0, jitter_ratio=0.0)
    assert policy.delay_before_attempt(1, retry_after=30.0) == 30.0


def test_budget_stops_execute_run(tmp_path, monkeypatch):
    from yt2class.config import CourseConfig
    from yt2class.orchestration.pipeline import PipelinePaused, execute_run
    from tests.helpers.m5 import build_evidence_bundle

    workspace, bundle = build_evidence_bundle(tmp_path)
    cfg = CourseConfig()
    cfg = cfg.model_copy(update={"budget": cfg.budget.model_copy(update={"max_model_calls": 0})})
    with pytest.raises(PipelinePaused):
        execute_run(
            tmp_path,
            build=__import__("yt2class.config", fromlist=["BuildSource"]).BuildSource(
                source_id=bundle.source_id
            ),
            config=cfg,
            run_id=workspace.root.name,
            evidence_bundle=bundle,
            stop_after="reduce_knowledge",
        )


def test_non_retryable_http_codes():
    assert classify_http_status(401) is NonRetryableError
    assert classify_http_status(429) is RetryableError
    assert classify_http_status(503) is RetryableError


def test_parse_retry_after_header():
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after(None) is None
