"""Unit tests for OpenRouterProvider (httpx/respx; no live API)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from yt2class.adapters.providers.base import MissingStructuredOutput, ProviderError
from yt2class.adapters.providers.openrouter import OpenRouterProvider
from yt2class.config import AnalysisConfig
from yt2class.domain.visual import VisualCatalogue
from yt2class.adapters.providers.openrouter import DEFAULT_MAX_TOKENS
from yt2class.orchestration.retry import InvalidJsonResponse, NonRetryableError, RetryableError
from yt2class.stages.llm_util import attach_provider_payload, model_request, payload_digest

ENDPOINT = "https://openrouter.test/v1/chat/completions"


def _provider(*, client: httpx.Client | None = None, json_mode: str = "auto") -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="test-key",
        model="google/gemma-4-31b-it:free",
        endpoint=ENDPOINT,
        client=client,
        json_mode=json_mode,  # type: ignore[arg-type]
    )


def _outline_request(payload: dict) -> tuple[OpenRouterProvider, object]:
    provider = _provider()
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    return provider, request


def test_openrouter_missing_api_key_raises():
    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider(api_key="", model="google/gemma-4-31b-it:free", endpoint=ENDPOINT)


def test_openrouter_successful_structured_json():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001", "start_seconds": 0.0, "end_seconds": 1.0},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    body = {
        "choices": [{"message": {"content": json.dumps({"topics": [], "relations": [], "unverified_guesses": []})}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }

    transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json=body, request=req)
    )
    provider = _provider(client=httpx.Client(transport=transport))
    provider.last_payload = payload

    result = provider.complete(request)
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 4


def test_openrouter_prefers_thread_payload_over_shared_last_payload():
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


def test_openrouter_success_includes_response_format_by_default():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                "usage": {},
            },
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    provider.complete(request)
    body = captured["body"]
    assert isinstance(body, dict)
    assert body.get("response_format") == {"type": "json_object"}


def test_openrouter_json_mode_off_omits_response_format():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                "usage": {},
            },
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)), json_mode="off")
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    provider.complete(request)
    body = captured["body"]
    assert isinstance(body, dict)
    assert "response_format" not in body


def test_openrouter_auto_retries_without_response_format_on_structured_400():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "model: inclusionai/ling-3.0-flash-vl does not support feature: structured-outputs"
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2},
            },
        )

    provider = _provider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        json_mode="auto",
    )
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    result = provider.complete(request)
    assert len(bodies) == 2
    assert "response_format" in bodies[0]
    assert "response_format" not in bodies[1]
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}


def test_resolve_openrouter_json_mode_from_env(monkeypatch):
    from yt2class.adapters.providers.openrouter import resolve_openrouter_json_mode

    monkeypatch.setenv("OPENROUTER_JSON_MODE", "off")
    assert resolve_openrouter_json_mode(AnalysisConfig(provider="openrouter")) == "off"


def test_openrouter_appends_role_json_reminder_after_payload():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics":[],"relations":[],"unverified_guesses":[]}'}}],
                "usage": {},
            },
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)), json_mode="off")
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    provider.complete(request)
    body = captured["body"]
    assert isinstance(body, dict)
    text = body["messages"][0]["content"][0]["text"]
    payload_at = text.rfind("Payload:")
    assert payload_at != -1
    reminder = text[payload_at:]
    assert "goal" in reminder
    assert "teaching_goal" in reminder
    assert "student_goal" in reminder


@respx.mock
def test_openrouter_trailing_extra_json_is_parsed():
    """Ling edit_deck: complete JSON object plus trailing Extra data must parse."""
    payload = {
        "prompt": "editor",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    content = (
        '{"topics": [], "relations": [], "unverified_guesses": []}'
        '{"duplicate": true} trailing } prose'
    )
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": content}}], "usage": {}},
        )
    )
    provider._client = httpx.Client()
    result = provider.complete(request)
    assert result.structured == {"topics": [], "relations": [], "unverified_guesses": []}


@respx.mock
def test_openrouter_truncated_json_raises_retryable_invalid_json():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"topics": [{"title": "unterminated'}}],
                "usage": {},
            },
        )
    )
    provider._client = httpx.Client()
    with pytest.raises(InvalidJsonResponse, match="truncated"):
        provider.complete(request)


@respx.mock
def test_openrouter_request_sets_max_tokens():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": json.dumps({"topics": [], "relations": [], "unverified_guesses": []})}}
                ],
                "usage": {},
            },
            request=request,
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    provider.complete(request)
    assert captured["body"]["max_tokens"] == DEFAULT_MAX_TOKENS


@respx.mock
def test_openrouter_bad_json_raises_missing_structured():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": "not json at all"}}], "usage": {}},
        )
    )
    provider._client = httpx.Client()
    with pytest.raises(MissingStructuredOutput, match="invalid JSON"):
        provider.complete(request)


@respx.mock
def test_openrouter_error_includes_metadata_raw():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    respx.post(ENDPOINT).mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "message": "Provider returned error",
                    "code": 429,
                    "metadata": {"raw": "rate limited on free tier", "provider_name": "Google"},
                }
            },
        )
    )
    provider._client = httpx.Client()
    with pytest.raises(RetryableError, match="metadata.raw=rate limited on free tier") as info:
        provider.complete(request)
    assert "Provider returned error" in str(info.value)


@respx.mock
def test_openrouter_http_401_is_non_retryable():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    respx.post(ENDPOINT).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    provider._client = httpx.Client()
    with pytest.raises(NonRetryableError, match="bad key"):
        provider.complete(request)


@respx.mock
def test_openrouter_http_429_is_retryable():
    payload = {
        "prompt": "outline",
        "block": {"id": "block-0001"},
        "transcript": [],
        "visual_overview": [],
        "allowed_evidence_ids": [],
        "constraints": {},
    }
    provider, request = _outline_request(payload)
    route = respx.post(ENDPOINT).mock(
        return_value=httpx.Response(429, headers={"Retry-After": "2"}, json={"error": {"message": "rate limit"}})
    )
    provider._client = httpx.Client()
    with pytest.raises(RetryableError, match="rate limit") as info:
        provider.complete(request)
    assert info.value.status_code == 429
    assert info.value.retry_after == 2.0
    assert route.call_count == 1


def test_openrouter_payload_digest_mismatch():
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
    provider.last_payload = {**payload, "extra": True}
    with pytest.raises(ProviderError, match="digest"):
        provider.complete(request)


def test_resolve_openrouter_model_prefers_config():
    from yt2class.adapters.providers.openrouter import resolve_openrouter_model

    analysis = AnalysisConfig(provider="openrouter", model="vendor/custom")
    assert resolve_openrouter_model(analysis) == "vendor/custom"


def test_openrouter_attaches_frame_images_from_run_root(tmp_path: Path):
    run_root = tmp_path / "run"
    frames_dir = run_root / "frames"
    frames_dir.mkdir(parents=True)
    image_path = frames_dir / "frame.jpg"
    image_path.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)

    visual = VisualCatalogue.model_validate(
        {
            "schema_version": "1.0",
            "source_id": "src-1",
            "scenes": [
                {
                    "id": "scene-1",
                    "start_seconds": 0.0,
                    "end_seconds": 1.0,
                    "detector": "content",
                }
            ],
            "assets": [
                {
                    "id": "asset-1",
                    "role": "frame",
                    "path": "frames/frame.jpg",
                    "sha256": "a" * 64,
                    "mime_type": "image/jpeg",
                    "width": 10,
                    "height": 10,
                }
            ],
            "occurrences": [
                {
                    "id": "frame-0001",
                    "scene_id": "scene-1",
                    "asset_id": "asset-1",
                    "requested_seconds": 0.0,
                    "timestamp_seconds": 0.0,
                    "quality": {
                        "width": 10,
                        "height": 10,
                        "brightness": 0.5,
                        "sharpness": 0.5,
                        "ocr_density": 0.0,
                    },
                }
            ],
            "ocr_regions": [],
            "gaps": [],
            "status": "complete",
        }
    )
    payload = {
        "prompt": "segment",
        "segment_id": "win-1",
        "frames": [{"id": "frame-0001", "timestamp_seconds": 0.0, "ocr_text": ""}],
        "allowed_frame_ids": ["frame-0001"],
        "allowed_evidence_ids": ["frame-0001"],
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
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    provider = _provider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    provider.bind_run_context(run_root, visual=visual)
    provider.last_payload = payload
    request = model_request(
        request_id="segment:win-1",
        role="segment",
        payload=payload,
        image_count=1,
    )
    provider.complete(request)
    body = captured["json"]
    assert isinstance(body, dict)
    messages = body["messages"]
    content = messages[0]["content"]
    assert any(part.get("type") == "image_url" for part in content if isinstance(part, dict))
    assert payload_digest(payload) == request.payload_digest


def test_openrouter_read_timeout_is_retryable():
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

    provider = OpenRouterProvider(
        api_key="test-key",
        model="google/gemma-4-31b-it:free",
        endpoint=ENDPOINT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        timeout_seconds=5.0,
    )
    provider.last_payload = payload
    request = model_request(request_id="outline:block-0001", role="outline", payload=payload)
    with pytest.raises(RetryableError, match="read timed out"):
        provider.complete(request)


def test_resolve_openrouter_timeout_seconds_from_config():
    from yt2class.adapters.providers.openrouter import resolve_openrouter_timeout_seconds

    analysis = AnalysisConfig(provider="openrouter", openrouter_timeout_seconds=120.0)
    assert resolve_openrouter_timeout_seconds(analysis) == 120.0
