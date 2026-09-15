"""Unit tests for VolcengineArkPlanProvider (httpx mocks; no live API)."""

from __future__ import annotations

import json

import httpx
import pytest

from yt2class.adapters.providers.base import ProviderError
from yt2class.adapters.providers.factory import resolve_course_provider
from yt2class.adapters.providers.volcengine_ark_plan import (
    VolcengineArkPlanProvider,
    resolve_volcengine_ark_model,
)
from yt2class.config import AnalysisConfig
from yt2class.orchestration.retry import RetryableError
from yt2class.stages.llm_util import model_request

ENDPOINT = "https://ark.test/api/plan/v3/chat/completions"


def _provider(*, client: httpx.Client | None = None, json_mode: str = "auto") -> VolcengineArkPlanProvider:
    return VolcengineArkPlanProvider(
        api_key="test-ark-key",
        model="ark-code-latest",
        endpoint=ENDPOINT,
        client=client,
        json_mode=json_mode,  # type: ignore[arg-type]
    )


def test_ark_missing_api_key_raises():
    with pytest.raises(ProviderError, match="VOLCENGINE_ARK_API_KEY"):
        VolcengineArkPlanProvider(api_key="", model="ark-code-latest", endpoint=ENDPOINT)


def test_ark_successful_structured_json():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider = _provider()
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    body = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {"topics": [], "relations": [], "unverified_guesses": []}
                    )
                }
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 5},
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=body, request=req)
    )
    provider = _provider(client=httpx.Client(transport=transport))
    provider.last_payload = payload
    result = provider.complete(request)
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}
    assert result.usage.input_tokens == 12


def test_ark_uses_reasoning_content_when_content_empty():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider = _provider()
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    reasoning_json = json.dumps({"topics": [], "relations": [], "unverified_guesses": []})
    body = {
        "choices": [{"message": {"content": "", "reasoning_content": reasoning_json}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=body, request=req)
    )
    provider = _provider(client=httpx.Client(transport=transport))
    provider.last_payload = payload
    result = provider.complete(request)
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}


def test_ark_read_timeout_is_retryable():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out", request=request)

    provider = VolcengineArkPlanProvider(
        api_key="test-ark-key",
        model="ark-code-latest",
        endpoint=ENDPOINT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=5.0,
    )
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    with pytest.raises(RetryableError, match="read timed out"):
        provider.complete(request)


def test_resolve_ark_model_prefers_config():
    analysis = AnalysisConfig(provider="ark-plan", model="custom-ark-model")
    assert resolve_volcengine_ark_model(analysis) == "custom-ark-model"


def test_factory_resolves_volcengine_alias(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "secret")
    provider = resolve_course_provider("volcengine", AnalysisConfig(provider="volcengine"))
    assert isinstance(provider, VolcengineArkPlanProvider)


def test_budgeted_provider_forwards_last_payload_to_ark():
    from yt2class.orchestration.budget import BudgetLimits, RunBudget
    from yt2class.orchestration.provider_gate import wrap_provider

    inner = _provider()
    wrapped = wrap_provider(inner, RunBudget(BudgetLimits(max_model_calls=3)))
    payload = {"prompt": "x", "constraints": {}}
    wrapped.last_payload = payload
    assert inner.last_payload == payload
