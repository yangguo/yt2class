"""Contract tests for provider and renderer abstractions. No pipeline rewrite."""

from __future__ import annotations

from threading import Event

import pytest

from yt2class.adapters.providers.base import (
    ContextOverflow,
    FakeProvider,
    MissingStructuredOutput,
    MissingUsage,
    ModelRequest,
    ProviderCapabilities,
    RequestCancelled,
    RequestTimeout,
    UnsupportedModality,
    Usage,
)
from yt2class.adapters.render.base import FakeRenderer, RenderRequest


def frames_caps(**overrides) -> ProviderCapabilities:
    data = dict(
        supports_images=True,
        supports_video=False,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=8000,
        max_output_tokens=2000,
        max_images=8,
        max_video_seconds=0.0,
    )
    data.update(overrides)
    return ProviderCapabilities.model_validate(data)


def text_request(**overrides) -> ModelRequest:
    data = dict(
        request_id="req-1",
        role="outline",
        modalities=["text"],
        estimated_input_tokens=100,
        estimated_output_tokens=50,
    )
    data.update(overrides)
    return ModelRequest.model_validate(data)


def test_unsupported_video_modality():
    provider = FakeProvider(frames_caps())
    with pytest.raises(UnsupportedModality, match="video"):
        provider.complete(
            text_request(modalities=["text", "video"], video_seconds=12.0, request_id="req-video")
        )


def test_context_overflow():
    provider = FakeProvider(frames_caps(max_input_tokens=50))
    with pytest.raises(ContextOverflow, match="context"):
        provider.complete(text_request(estimated_input_tokens=4000))


def test_missing_structured_output_capability_and_result():
    provider = FakeProvider(frames_caps(supports_structured_output=False))
    with pytest.raises(MissingStructuredOutput, match="does not support"):
        provider.complete(text_request())
    provider = FakeProvider(frames_caps(), omit_structured=True)
    with pytest.raises(MissingStructuredOutput, match="missing structured"):
        provider.complete(text_request(request_id="req-unstructured"))


def test_cancel_via_event_and_provider_flag():
    provider = FakeProvider(frames_caps())
    cancel = Event()
    cancel.set()
    with pytest.raises(RequestCancelled, match="cancelled"):
        provider.complete(text_request(), cancel_event=cancel)
    provider = FakeProvider(frames_caps(), cancel=True)
    with pytest.raises(RequestCancelled):
        provider.complete(text_request(request_id="req-cancel"))


def test_timeout():
    provider = FakeProvider(frames_caps(), timeout=True)
    with pytest.raises(RequestTimeout, match="timed out"):
        provider.complete(text_request())


def test_missing_usage():
    provider = FakeProvider(frames_caps(), omit_usage=True)
    with pytest.raises(MissingUsage, match="usage"):
        provider.complete(text_request())


def test_idempotent_request_ids():
    provider = FakeProvider(frames_caps(), structured={"topic": "合成"})
    first = provider.complete(text_request(request_id="req-same"))
    second = provider.complete(
        text_request(request_id="req-same", estimated_input_tokens=999)
    )
    assert first is second
    assert first.usage == Usage(
        request_id="req-same",
        input_tokens=100,
        output_tokens=1,
        image_count=0,
        video_seconds=0.0,
    )
    third = provider.complete(text_request(request_id="req-other"))
    assert third.request_id == "req-other"
    assert third is not first


def test_render_request_is_idempotent_and_reports_usage_of_preview_policy():
    renderer = FakeRenderer()
    request = RenderRequest(
        request_id="render-1",
        spec_path="delivery/slide-spec.json",
        run_root="/tmp/run",
        output_path="delivery/lesson.pptx",
        preview_policy="off",
    )
    first = renderer.render(request)
    second = renderer.render(request)
    assert first is second
    assert first.request_id == "render-1"
    assert first.visual_qa == "unavailable"
    assert first.render_complete is True
