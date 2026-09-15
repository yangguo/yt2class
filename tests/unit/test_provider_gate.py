"""BudgetedProvider forwarding and budget gate behavior."""

from __future__ import annotations

import httpx
import pytest

from yt2class.adapters.providers.base import ModelRequest, ModelResult, Provider, Usage
from yt2class.adapters.providers.openrouter import OpenRouterProvider
from yt2class.orchestration.budget import BudgetLimits, RunBudget
from yt2class.orchestration.provider_gate import BudgetedProvider, wrap_provider
from yt2class.stages.llm_util import model_request
from tests.contract.test_adapters import frames_caps


def test_budgeted_provider_forwards_last_payload_to_inner():
    inner = OpenRouterProvider(
        api_key="test-key",
        model="google/gemma-4-31b-it:free",
        endpoint="https://openrouter.test/v1/chat/completions",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                    },
                )
            )
        ),
    )
    wrapped = wrap_provider(inner, RunBudget(BudgetLimits(max_model_calls=5)))
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    assert hasattr(wrapped, "last_payload")
    wrapped.last_payload = payload
    assert inner.last_payload is payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    result = wrapped.complete(request)
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}


def test_budgeted_provider_last_payload_hasattr_false_for_plain_provider():
    class PlainProvider(Provider):
        def __init__(self) -> None:
            super().__init__(frames_caps())

        def _complete(self, request: ModelRequest, *, cancel_event=None) -> ModelResult:
            return ModelResult(
                request_id=request.request_id,
                structured={"ok": True},
                usage=Usage(request_id=request.request_id, input_tokens=1, output_tokens=1),
            )

    wrapped = BudgetedProvider(PlainProvider(), RunBudget(BudgetLimits(max_model_calls=1)))
    assert not hasattr(wrapped, "last_payload")


def test_outline_attach_payload_pattern_reaches_openrouter_inner():
    """Mirrors outline._attach_payload + pipeline wrap_provider."""

    inner = OpenRouterProvider(
        api_key="test-key",
        model="google/gemma-4-31b-it:free",
        endpoint="https://openrouter.test/v1/chat/completions",
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                        "usage": {},
                    },
                )
            )
        ),
    )
    provider = wrap_provider(inner, RunBudget(BudgetLimits(max_model_calls=2)))
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }

    def attach_payload(subject, body: dict) -> None:
        if hasattr(subject, "last_payload"):
            subject.last_payload = body

    attach_payload(provider, payload)
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    wrapped = provider.complete(request)
    assert wrapped.structured is not None
    assert inner.last_payload is payload
