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


def test_non_retryable_http_codes():
    assert classify_http_status(401) is NonRetryableError
    assert classify_http_status(429) is RetryableError
    assert classify_http_status(503) is RetryableError


def test_parse_retry_after_header():
    assert parse_retry_after("2.5") == 2.5
    assert parse_retry_after(None) is None
