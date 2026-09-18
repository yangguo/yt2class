from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from yt2class.adapters.providers.base import FakeProvider, ModelResult, Usage
from yt2class.orchestration.budget import BudgetExceeded, BudgetLimits, RunBudget
from yt2class.orchestration.provider_gate import BudgetedProvider
from yt2class.stages.llm_util import model_request
from yt2class.stages.verify_claims import _complete_verifier
from tests.helpers.m2 import frames_caps


def _verifier_request():
    return model_request(
        request_id="gate:test",
        role="verifier",
        payload={"claims": [], "evidence": {}},
    )


def test_budgeted_provider_preflight_blocks_before_provider_call():
    inner = FakeProvider(frames_caps())
    budget = RunBudget(limits=BudgetLimits(max_model_calls=0))
    wrapped = BudgetedProvider(inner, budget)
    request = _verifier_request()
    assert not inner.requests
    with pytest.raises(BudgetExceeded):
        wrapped.complete(request)
    assert not inner.requests


def test_budgeted_provider_returns_result_when_post_call_charge_exceeds():
    class HeavyUsageProvider(FakeProvider):
        def _complete(self, request, *, cancel_event=None):
            result = super()._complete(request, cancel_event=cancel_event)
            if request.request_id == "gate:2":
                result = result.model_copy(
                    update={
                        "usage": result.usage.model_copy(update={"input_tokens": 8}),
                    }
                )
            return result

    inner = HeavyUsageProvider(frames_caps(), structured={"ok": True})
    budget = RunBudget(limits=BudgetLimits(max_input_tokens=12))
    wrapped = BudgetedProvider(inner, budget)
    first = _verifier_request().model_copy(
        update={"request_id": "gate:1", "estimated_input_tokens": 7, "estimated_output_tokens": 1}
    )
    second = _verifier_request().model_copy(
        update={"request_id": "gate:2", "estimated_input_tokens": 1, "estimated_output_tokens": 1}
    )
    first = wrapped.complete(first)
    assert first.structured == {"ok": True}
    second = wrapped.complete(second)
    assert second.structured == {"ok": True}
    assert budget.paused is True
    assert budget.consumed.input_tokens == 15


def test_budgeted_provider_atomically_admits_parallel_requests():
    class SlowProvider(FakeProvider):
        def __init__(self):
            super().__init__(frames_caps())
            self.started = threading.Event()
            self.release = threading.Event()

        def _complete(self, request, *, cancel_event=None):
            self.started.set()
            assert self.release.wait(timeout=1.0)
            return super()._complete(request, cancel_event=cancel_event)

    inner = SlowProvider()
    budget = RunBudget(limits=BudgetLimits(max_model_calls=1))
    wrapped = BudgetedProvider(inner, budget)
    requests = [
        _verifier_request().model_copy(update={"request_id": f"gate:parallel:{index}"})
        for index in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted = pool.submit(wrapped.complete, requests[0])
        assert inner.started.wait(timeout=1.0)
        rejected = pool.submit(wrapped.complete, requests[1])
        with pytest.raises(BudgetExceeded):
            rejected.result(timeout=1.0)
        inner.release.set()
        assert isinstance(admitted.result(timeout=1.0), ModelResult)

    assert len(inner.requests) == 1
    assert budget.consumed.model_calls == 1


def test_verifier_propagates_budget_pause():
    inner = FakeProvider(frames_caps())
    budget = RunBudget(limits=BudgetLimits(max_model_calls=0))
    wrapped = BudgetedProvider(inner, budget)

    with pytest.raises(BudgetExceeded):
        _complete_verifier(
            wrapped,
            {"claims": []},
            request_id="gate:verifier-budget",
            cancel_event=None,
        )


def test_budgeted_provider_charges_idempotent_request_once():
    inner = FakeProvider(frames_caps())
    budget = RunBudget(limits=BudgetLimits(max_model_calls=2))
    wrapped = BudgetedProvider(inner, budget)
    request = _verifier_request()

    first = wrapped.complete(request)
    second = wrapped.complete(request)

    assert second is first
    assert len(inner.requests) == 1
    assert budget.consumed.model_calls == 1


def test_budgeted_provider_settles_reported_media_usage():
    class MediaUsageProvider(FakeProvider):
        def _complete(self, request, *, cancel_event=None):
            result = super()._complete(request, cancel_event=cancel_event)
            return result.model_copy(
                update={
                    "usage": result.usage.model_copy(
                        update={"image_count": 3, "video_seconds": 4.0}
                    )
                }
            )

    inner = MediaUsageProvider(frames_caps())
    budget = RunBudget(limits=BudgetLimits(max_images=2, max_video_seconds=3.0))
    wrapped = BudgetedProvider(inner, budget)

    result = wrapped.complete(_verifier_request())

    assert result.usage.image_count == 3
    assert budget.consumed.image_count == 3
    assert budget.consumed.video_seconds == 4.0
    assert budget.paused is True
