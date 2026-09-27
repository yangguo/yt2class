"""Provider-neutral image configuration and OpenAI-compatible vision transport."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.helpers.m5 import build_evidence_bundle
from yt2class.adapters.providers.factory import resolve_course_provider
from yt2class.adapters.providers.base import ProviderError
from yt2class.adapters.providers.openai_compatible_image import OpenAICompatibleImageProvider
from yt2class.adapters.providers.openrouter import resolve_openrouter_model
from yt2class.adapters.providers.volcengine_ark_plan import resolve_volcengine_ark_model
from yt2class.config import AnalysisConfig, load_config
from yt2class.llm import ModelConfig
from yt2class.stages.llm_util import model_request


def test_image_environment_selects_provider_and_model_without_vendor_names(monkeypatch):
    for key in ("YT2CLASS_MODEL_URL", "YT2CLASS_MODEL_KEY", "YT2CLASS_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("YT2CLASS_IMAGE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "vision-choice")
    monkeypatch.setenv("YT2CLASS_IMAGE_ENDPOINT", "https://vision.test/v1/chat/completions")
    monkeypatch.setenv("YT2CLASS_IMAGE_API_KEY", "test-key")

    config = load_config(None)

    assert config.analysis.provider == "openai-compatible"
    assert config.analysis.model == "vision-choice"
    assert isinstance(resolve_course_provider(config.analysis.provider, config.analysis), OpenAICompatibleImageProvider)
    assert ModelConfig.from_environment() == ModelConfig(
        endpoint="https://vision.test/v1/chat/completions",
        api_key="test-key",
        model="vision-choice",
    )


def test_course_config_values_beat_image_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2CLASS_IMAGE_PROVIDER", "openai-compatible")
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "env-model")
    config_file = tmp_path / "course.json"
    config_file.write_text(
        json.dumps({"analysis": {"provider": "openrouter", "model": "course-model"}}),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.analysis.provider == "openrouter"
    assert config.analysis.model == "course-model"


def test_unset_course_model_uses_shared_image_model(tmp_path, monkeypatch):
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "env-model")
    config_file = tmp_path / "course.json"
    config_file.write_text(
        json.dumps({"analysis": {"provider": "openrouter", "model": None}}),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.analysis.provider == "openrouter"
    assert config.analysis.model == "env-model"


def test_shared_image_settings_take_precedence_over_legacy_settings(monkeypatch):
    monkeypatch.setenv("YT2CLASS_MODEL_URL", "https://old.test/v1/chat/completions")
    monkeypatch.setenv("YT2CLASS_MODEL_KEY", "old-key")
    monkeypatch.setenv("YT2CLASS_MODEL", "old-model")
    monkeypatch.setenv("YT2CLASS_IMAGE_ENDPOINT", "https://new.test/v1/chat/completions")
    monkeypatch.setenv("YT2CLASS_IMAGE_API_KEY", "new-key")
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "new-model")

    config = ModelConfig.from_environment()

    assert config is not None
    assert (config.endpoint, config.api_key, config.model) == (
        "https://new.test/v1/chat/completions", "new-key", "new-model"
    )


def test_shared_image_model_applies_to_existing_provider_adapters(monkeypatch):
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "shared-vision-model")
    monkeypatch.setenv("YT2CLASS_OPENROUTER_MODEL", "old-router-model")
    monkeypatch.setenv("YT2CLASS_ARK_MODEL", "old-ark-model")

    assert resolve_openrouter_model(AnalysisConfig(provider="openrouter")) == "shared-vision-model"
    assert resolve_volcengine_ark_model(AnalysisConfig(provider="ark-plan")) == "shared-vision-model"


def test_openai_compatible_provider_sends_original_frame_to_configured_endpoint(tmp_path, monkeypatch):
    workspace, bundle = build_evidence_bundle(tmp_path)
    frame = bundle.visual.occurrences[0]
    monkeypatch.setenv("YT2CLASS_IMAGE_ENDPOINT", "https://vision.test/v1/chat/completions")
    monkeypatch.setenv("YT2CLASS_IMAGE_API_KEY", "test-key")
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "vision-choice")
    captured = {}

    def respond(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"units":[]}'}}], "usage": {}},
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(respond))
    provider = OpenAICompatibleImageProvider.from_config(load_config(None).analysis, client=client)
    provider.bind_run_context(workspace.root, visual=bundle.visual)
    payload = {
        "prompt": "analyze original frame",
        "frames": [{"id": frame.id, "timestamp_seconds": frame.timestamp_seconds}],
        "allowed_evidence_ids": [frame.id],
    }
    provider.last_payload = payload
    request = model_request(request_id="segment:frame", role="segment", payload=payload, image_count=1)

    result = provider.complete(request)

    assert result.structured == {"units": []}
    assert captured["url"] == "https://vision.test/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer test-key"
    assert "http-referer" not in captured["headers"]
    assert captured["body"]["model"] == "vision-choice"
    content = captured["body"]["messages"][0]["content"]
    assert any(part.get("type") == "image_url" and part["image_url"]["url"].startswith("data:image/") for part in content)


def test_generic_provider_error_does_not_claim_openrouter(monkeypatch):
    monkeypatch.setenv("YT2CLASS_IMAGE_ENDPOINT", "https://vision.test/v1/chat/completions")
    monkeypatch.setenv("YT2CLASS_IMAGE_API_KEY", "test-key")
    monkeypatch.setenv("YT2CLASS_IMAGE_MODEL", "vision-choice")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}, request=request)))
    provider = OpenAICompatibleImageProvider.from_config(load_config(None).analysis, client=client)
    payload = {"prompt": "outline", "allowed_evidence_ids": []}
    provider.last_payload = payload
    request = model_request(request_id="outline:one", role="outline", payload=payload)

    with pytest.raises(ProviderError, match="OpenAI-compatible") as error:
        provider.complete(request)

    assert "OpenRouter" not in str(error.value)
