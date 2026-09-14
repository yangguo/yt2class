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
from yt2class.orchestration.retry import NonRetryableError, RetryableError
from yt2class.stages.llm_util import model_request, payload_digest

ENDPOINT = "https://openrouter.test/v1/chat/completions"


def _provider(*, client: httpx.Client | None = None) -> OpenRouterProvider:
    return OpenRouterProvider(
        api_key="test-key",
        model="google/gemma-4-31b-it:free",
        endpoint=ENDPOINT,
        client=client,
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
