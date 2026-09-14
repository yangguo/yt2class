"""Contract tests for native video upload / analyze / delete (fake backend only)."""

from __future__ import annotations

from pathlib import Path
from threading import Event

import pytest

from yt2class.adapters.providers.base import ProviderCapabilities, RequestCancelled
from yt2class.adapters.providers.base import UnsupportedModality
from yt2class.adapters.providers.native_video import (
    NATIVE_VIDEO_DOC_VERSION,
    DeleteRemoteFailed,
    FakeNativeVideoBackend,
    NativeVideoAdapter,
    NativeVideoError,
    fake_native_adapter,
    hash_file,
    redact_handle,
    sniff_mime,
)
from yt2class.domain.media_audit import MediaClipRange


def _write_clip(path: Path, payload: bytes = b"fake-video-bytes") -> Path:
    path.write_bytes(payload)
    return path


def test_sniff_mime_and_duration_on_clip():
    clip = _write_clip(Path("clip.mp4"))
    assert sniff_mime(clip) == "video/mp4"
    digest = hash_file(clip)
    assert len(digest) == 64
    adapter = fake_native_adapter()
    upload = adapter.prepare_clip_upload(
        upload_id="up-1",
        media_path=clip,
        start_seconds=10.0,
        end_seconds=25.0,
        segment_id="seg-1",
    )
    assert upload.duration_seconds == 15.0
    assert upload.byte_length == len(b"fake-video-bytes")
    assert upload.local_sha256 == digest


def test_redacted_provider_handle_never_contains_raw_secret():
    raw = "secret-signed-url-token-abc"
    digest = redact_handle("vendor-x", raw)
    assert raw not in digest
    assert len(digest) == 64


def test_upload_analyze_delete_records_usage_and_probe():
    clip_path = _write_clip(Path("sample.webm"))
    backend = FakeNativeVideoBackend(
        structured={"units": []},
        approximate_seconds=[12.5, 13.0],
    )
    adapter = fake_native_adapter(backend=backend)
    assert adapter.probe is not None
    assert adapter.probe.doc_version == NATIVE_VIDEO_DOC_VERSION
    assert adapter.probe.supports_video is True
    clip = adapter.prepare_clip_upload(
        upload_id="up-usage",
        media_path=clip_path,
        start_seconds=0.0,
        end_seconds=8.0,
    )
    analysis = adapter.upload_and_analyze(clip, prompt_digest="a" * 64)
    assert analysis.usage.video_seconds == 8.0
    assert analysis.usage.estimated_usd == 0.01
    assert len(analysis.approximate_timestamps) == 2
    assert backend.deleted == ["up-usage"]
    assert adapter.audit_records[-1].state == "deleted"
    assert adapter.audit_records[-1].remote is not None
    assert adapter.audit_records[-1].remote.handle_digest == redact_handle(
        "fake-native", "fh-up-usage"
    )


def test_cancel_during_upload_marks_failed():
    backend = FakeNativeVideoBackend(cancel_on_upload=True)
    adapter = fake_native_adapter(backend=backend)
    clip_path = _write_clip(Path("cancel.mp4"))
    clip = adapter.prepare_clip_upload(
        upload_id="up-cancel",
        media_path=clip_path,
        start_seconds=0.0,
        end_seconds=6.0,
    )
    cancel = Event()
    cancel.set()
    with pytest.raises(RequestCancelled):
        adapter.upload_and_analyze(clip, prompt_digest="b" * 64, cancel_event=cancel)
    assert adapter.audit_records[-1].state == "failed"


def test_delete_failure_records_retention_state():
    backend = FakeNativeVideoBackend(fail_delete=True)
    caps = ProviderCapabilities(
        supports_images=True,
        supports_video=True,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=8000,
        max_output_tokens=2000,
        max_images=0,
        max_video_seconds=60.0,
        remote_retention="ephemeral",
        can_delete_remote=True,
    )
    adapter = fake_native_adapter(capabilities=caps, backend=backend)
    clip_path = _write_clip(Path("retain.mp4"))
    clip = adapter.prepare_clip_upload(
        upload_id="up-del-fail",
        media_path=clip_path,
        start_seconds=1.0,
        end_seconds=10.0,
    )
    adapter.upload_and_analyze(clip, prompt_digest="c" * 64)
    assert adapter.audit_records[-1].state in {"delete_failed", "retained"}
    assert "simulated delete" in adapter.audit_records[-1].note


def test_unavailable_backend_raises_unsupported():
    caps = ProviderCapabilities(
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
    adapter = NativeVideoAdapter(
        provider_name="disabled",
        capabilities=caps,
        backend=None,
    )
    assert adapter.available is False
    clip_path = _write_clip(Path("noop.mp4"))
    clip = adapter.prepare_clip_upload(
        upload_id="up-no",
        media_path=clip_path,
        start_seconds=0.0,
        end_seconds=6.0,
    )
    with pytest.raises(UnsupportedModality):
        adapter.upload_and_analyze(clip, prompt_digest="d" * 64)


def test_clip_range_validation():
    clip_path = _write_clip(Path("range.mp4"))
    adapter = fake_native_adapter()
    with pytest.raises(Exception):
        adapter.prepare_clip_upload(
            upload_id="bad",
            media_path=clip_path,
            start_seconds=5.0,
            end_seconds=5.0,
        )


def test_media_clip_range_model():
    row = MediaClipRange(start_seconds=1.0, end_seconds=6.0, segment_id="seg")
    assert row.end_seconds - row.start_seconds == 5.0
