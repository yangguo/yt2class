"""Unit tests for VolcengineArkPlanProvider (httpx mocks; no live API)."""

from __future__ import annotations

import json
import time

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
from yt2class.stages.llm_util import attach_provider_payload, model_request

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


def test_ark_request_includes_reasoning_effort_none():
    payload = {
        "prompt": "segment",
        "segment_id": "win-1",
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"units": []})}}],
                "usage": {},
            },
            request=request,
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.last_payload = payload
    request = model_request(request_id="seg:win-1", role="segment", payload=payload)
    provider.complete(request)
    body = captured["json"]
    assert isinstance(body, dict)
    assert body.get("reasoning") == {"effort": "none"}
    assert body.get("max_tokens", 0) >= 16_384


def test_ark_length_truncation_retries_with_higher_max_tokens():
    payload = {
        "prompt": "segment",
        "segment_id": "win-1",
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "model": "glm-5-3-flash",
                    "choices": [
                        {
                            "finish_reason": "length",
                            "message": {
                                "content": "",
                                "reasoning_content": "extended internal reasoning without json",
                            },
                        }
                    ],
                    "usage": {"completion_tokens_details": {"reasoning_tokens": 8089}},
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "model": "glm-5-3-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"units": []})},
                    }
                ],
                "usage": {},
            },
            request=request,
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.last_payload = payload
    request = model_request(request_id="seg:win-1", role="segment", payload=payload)
    result = provider.complete(request)
    assert result.structured == {"units": []}
    assert len(bodies) == 2
    assert bodies[0].get("max_tokens") == 16_384
    assert bodies[1].get("max_tokens") == 32_768
    assert bodies[1].get("reasoning") == {"effort": "none"}
    retry_text = bodies[1]["messages"][0]["content"][0]["text"]
    assert "truncated" in retry_text.lower()


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


def test_ark_prefers_thread_payload_over_shared_last_payload():
    payload = {
        "prompt": "outline-a",
        "block": {"id": "block-a"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    response = {
        "choices": [
            {
                "message": {
                    "content": '{"topics":[],"relations":[],"unverified_guesses":[]}'
                }
            }
        ],
        "usage": {},
    }
    provider = _provider(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response, request=request)
            )
        )
    )
    attach_provider_payload(provider, payload)
    provider.last_payload = {**payload, "prompt": "outline-b"}
    request = model_request(request_id="outline:block-a", role="outline", payload=payload)

    result = provider.complete(request)

    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}


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


def test_ark_wall_clock_deadline_aborts_slow_handler():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(2.0)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"topics": [], "relations": [], "unverified_guesses": []}
                            )
                        }
                    }
                ],
                "usage": {},
            },
            request=request,
        )

    provider = VolcengineArkPlanProvider(
        api_key="test-ark-key",
        model="ark-code-latest",
        endpoint=ENDPOINT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=0.25,
    )
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    started = time.monotonic()
    with pytest.raises(RetryableError, match="wall-clock deadline"):
        provider.complete(request)
    assert time.monotonic() - started < 1.5


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


def test_ark_from_config_caps_max_images_per_batch(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VOLCENGINE_ARK_API_KEY", "secret")
    analysis = AnalysisConfig(
        provider="ark-plan",
        max_images_per_batch=8,
        ark_max_images_per_batch=3,
    )
    provider = VolcengineArkPlanProvider.from_config(analysis)
    assert provider.capabilities.max_images == 3


def test_ark_outline_trims_visual_overview_in_prompt():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [{"id": f"f-{index}"} for index in range(12)],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {"topics": [], "relations": [], "unverified_guesses": []}
                            )
                        }
                    }
                ],
                "usage": {},
            },
            request=request,
        )

    provider = VolcengineArkPlanProvider(
        api_key="test-ark-key",
        model="ark-code-latest",
        endpoint=ENDPOINT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        outline_max_visual_overview=5,
    )
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    provider.complete(request)
    body = captured["json"]
    assert isinstance(body, dict)
    messages = body["messages"]
    text = messages[0]["content"][0]["text"]
    assert "visual_overview_omitted" in text
    assert '"id": "f-4"' in text
    assert '"id": "f-11"' not in text


def test_ark_segment_trims_dense_ocr_and_evidence_lists():
    ocr_rows = [
        {
            "id": f"ocr-{index:04d}",
            "parent_frame_id": "frame-0001",
            "text": "x" * 400,
        }
        for index in range(60)
    ]
    evidence_ids = [f"ocr-{index:04d}" for index in range(200)] + ["frame-0001", "seg-0001"]
    payload = {
        "prompt": "segment",
        "segment_id": "win-1",
        "evidence_ids": evidence_ids,
        "allowed_evidence_ids": evidence_ids,
        "allowed_frame_ids": ["frame-0001"],
        "frames": [{"id": "frame-0001", "timestamp_seconds": 0.0}],
        "ocr": ocr_rows,
        "transcript": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"units": []})}}],
                "usage": {},
            },
            request=request,
        )

    provider = VolcengineArkPlanProvider(
        api_key="test-ark-key",
        model="ark-code-latest",
        endpoint=ENDPOINT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        segment_max_ocr_regions=10,
        segment_max_evidence_ids=20,
        segment_ocr_text_chars=80,
    )
    provider.last_payload = payload
    request = model_request(request_id="seg:win-1", role="segment", payload=payload)
    provider.complete(request)
    body = captured["json"]
    assert isinstance(body, dict)
    text = body["messages"][0]["content"][0]["text"]
    assert "ocr_regions_omitted" in text
    assert "evidence_ids_omitted" in text
    assert "ocr-0059" not in text
    assert ("x" * 400) not in text


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
