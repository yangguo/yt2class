from __future__ import annotations

import pytest

from yt2class.adapters.providers.base import FakeProvider, ModelResult, Usage
from yt2class.orchestration.budget import BudgetExceeded, BudgetLimits, RunBudget
from yt2class.orchestration.provider_gate import BudgetedProvider
from yt2class.stages.llm_util import model_request
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
