"""Native video upload / readiness / analyze / delete mapped to source clip ranges."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import hashlib
import mimetypes
from pathlib import Path
from threading import Event
from typing import Any, Literal

from pydantic import Field

from yt2class.adapters.providers.base import (
    ProviderCapabilities,
    ProviderError,
    RequestCancelled,
    UnsupportedModality,
    Usage,
)
from yt2class.domain.common import Digest, Identifier, StrictModel, validate_half_open
from yt2class.domain.media_audit import (
    MediaClipRange,
    MediaUploadRecord,
    ProviderProbeRecord,
    RedactedRemoteHandle,
)

NATIVE_VIDEO_DOC_VERSION = "yt2class-native-video-adapter/1.0"

UploadState = Literal[
    "planned",
    "uploading",
    "ready",
    "analyzing",
    "analyzed",
    "deleted",
    "delete_failed",
    "retained",
    "failed",
]


class NativeVideoError(ProviderError):
    code = "native_video_error"


class DeleteRemoteFailed(NativeVideoError):
    code = "delete_remote_failed"


class NativeVideoEmptyOutput(NativeVideoError):
    code = "empty_native_output"


class ClipUpload(StrictModel):
    upload_id: Identifier
    local_path: str = Field(min_length=1, max_length=400)
    local_sha256: Digest
    mime_type: str = Field(min_length=3, max_length=80)
    byte_length: int = Field(ge=0)
    duration_seconds: float = Field(ge=0, allow_inf_nan=False)
    source_range: MediaClipRange


class NativeVideoHandle(StrictModel):
    upload_id: Identifier
    provider: str
    handle_digest: Digest
    state: UploadState
    source_range: MediaClipRange


class ApproximateTimestamp(StrictModel):
    """Model-suggested moment mapped back to source time (may be approximate)."""

    seconds: float = Field(ge=0, allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    note: str = Field(default="", max_length=240)


class NativeVideoAnalysis(StrictModel):
    upload_id: Identifier
    structured: dict[str, Any]
    usage: Usage
    approximate_timestamps: list[ApproximateTimestamp] = Field(default_factory=list)


class NativeVideoBackend(ABC):
    """Vendor-specific transport; fakes implement this for contract tests."""

    @abstractmethod
    def upload(
        self,
        clip: ClipUpload,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        raise NotImplementedError

    @abstractmethod
    def wait_ready(
        self,
        handle: NativeVideoHandle,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        raise NotImplementedError

    @abstractmethod
    def analyze(
        self,
        handle: NativeVideoHandle,
        *,
        prompt_digest: Digest,
        cancel_event: Event | None = None,
    ) -> NativeVideoAnalysis:
        raise NotImplementedError

    @abstractmethod
    def delete_remote(
        self,
        handle: NativeVideoHandle,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        raise NotImplementedError


def redact_handle(provider: str, raw_handle: str) -> Digest:
    digest = hashlib.sha256(f"{provider}:{raw_handle}".encode("utf-8")).hexdigest()
    return digest


def probe_capabilities(
    provider_name: str,
    capabilities: ProviderCapabilities,
) -> ProviderProbeRecord:
    return ProviderProbeRecord(
        provider=provider_name,
        doc_version=NATIVE_VIDEO_DOC_VERSION,
        supports_video=capabilities.supports_video,
        supports_images=capabilities.supports_images,
        max_video_seconds=capabilities.max_video_seconds,
        remote_retention=capabilities.remote_retention,
        can_delete_remote=capabilities.can_delete_remote,
    )


def sniff_mime(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    if guessed and guessed.startswith("video/"):
        return guessed
    suffix = path.suffix.lower()
    if suffix in {".mp4", ".m4v"}:
        return "video/mp4"
    if suffix == ".webm":
        return "video/webm"
    if suffix == ".mov":
        return "video/quicktime"
    if suffix == ".mkv":
        return "video/x-matroska"
    return guessed or "application/octet-stream"


def hash_file(path: Path) -> Digest:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class NativeVideoAdapter:
    """Maps local source clip ranges to provider upload / analyze / delete."""

    provider_name: str
    capabilities: ProviderCapabilities
    backend: NativeVideoBackend | None = None
    probe: ProviderProbeRecord | None = None
    _records_by_id: dict[str, MediaUploadRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.probe = probe_capabilities(self.provider_name, self.capabilities)
        if self.capabilities.supports_video and self.backend is None:
            raise UnsupportedModality(
                f"native video provider {self.provider_name!r} has no configured backend"
            )

    @property
    def available(self) -> bool:
        return self.capabilities.supports_video and self.backend is not None

    @property
    def audit_records(self) -> list[MediaUploadRecord]:
        return list(self._records_by_id.values())

    def prepare_clip_upload(
        self,
        *,
        upload_id: str,
        clip_path: Path,
        start_seconds: float,
        end_seconds: float,
        segment_id: str | None = None,
    ) -> ClipUpload:
        """Build upload metadata for an already-extracted clip file (not the full source)."""

        validate_half_open(start_seconds, end_seconds, label="clip range")
        if not clip_path.is_file():
            raise NativeVideoError(f"clip file missing: {clip_path}")
        clip_duration = max(0.0, end_seconds - start_seconds)
        return ClipUpload(
            upload_id=upload_id,
            local_path=str(clip_path),
            local_sha256=hash_file(clip_path),
            mime_type=sniff_mime(clip_path),
            byte_length=clip_path.stat().st_size,
            duration_seconds=clip_duration,
            source_range=MediaClipRange(
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                segment_id=segment_id,
            ),
        )

    def _upsert_record(
        self,
        clip: ClipUpload,
        *,
        state: str,
        remote: RedactedRemoteHandle | None = None,
        usage: Usage | None = None,
        note: str = "",
        bytes_sent: bool | None = None,
    ) -> MediaUploadRecord:
        previous = self._records_by_id.get(clip.upload_id)
        sent = bytes_sent if bytes_sent is not None else (previous.bytes_sent if previous else False)
        if state in {"uploaded", "ready", "analyzed", "deleted", "retained", "delete_failed"}:
            sent = True
        record = MediaUploadRecord(
            upload_id=clip.upload_id,
            local_sha256=clip.local_sha256,
            mime_type=clip.mime_type,
            byte_length=clip.byte_length,
            duration_seconds=clip.duration_seconds,
            source_range=clip.source_range,
            remote=remote if remote is not None else (previous.remote if previous else None),
            retention_policy=self.capabilities.remote_retention,
            state=state,  # type: ignore[arg-type]
            usage_input_tokens=usage.input_tokens if usage else (previous.usage_input_tokens if previous else 0),
            usage_output_tokens=usage.output_tokens if usage else (previous.usage_output_tokens if previous else 0),
            usage_video_seconds=(
                usage.video_seconds
                if usage
                else (previous.usage_video_seconds if previous else clip.duration_seconds)
            ),
            estimated_usd=usage.estimated_usd if usage else (previous.estimated_usd if previous else None),
            note=(note or (previous.note if previous else ""))[:400],
            bytes_sent=sent,
        )
        self._records_by_id[clip.upload_id] = record
        return record

    def _attempt_delete(
        self,
        backend: NativeVideoBackend,
        handle: NativeVideoHandle,
        clip: ClipUpload,
        *,
        cancel_event: Event | None,
        usage: Usage | None = None,
        keep_state: str | None = None,
    ) -> None:
        if not self.capabilities.can_delete_remote:
            self._upsert_record(
                clip,
                state="retained",
                remote=RedactedRemoteHandle(
                    provider=handle.provider,
                    handle_digest=handle.handle_digest,
                    state="retained",
                ),
                usage=usage,
                note="provider cannot delete remote objects",
            )
            return
        try:
            deleted = backend.delete_remote(handle, cancel_event=cancel_event)
            final = keep_state or "deleted"
            self._upsert_record(
                clip,
                state=final,  # type: ignore[arg-type]
                remote=RedactedRemoteHandle(
                    provider=deleted.provider,
                    handle_digest=deleted.handle_digest,
                    state=final,  # type: ignore[arg-type]
                ),
                usage=usage,
            )
        except DeleteRemoteFailed as error:
            retained_state = keep_state or (
                "retained" if self.capabilities.remote_retention != "none" else "delete_failed"
            )
            self._upsert_record(
                clip,
                state=retained_state,  # type: ignore[arg-type]
                remote=RedactedRemoteHandle(
                    provider=handle.provider,
                    handle_digest=handle.handle_digest,
                    state=retained_state,  # type: ignore[arg-type]
                ),
                usage=usage,
                note=str(error)[:400],
            )

    def upload_and_analyze(
        self,
        clip: ClipUpload,
        *,
        prompt_digest: Digest,
        cancel_event: Event | None = None,
        delete_after: bool = True,
    ) -> NativeVideoAnalysis:
        if not self.available:
            raise UnsupportedModality("native video adapter is not available")
        if clip.duration_seconds > self.capabilities.max_video_seconds:
            raise NativeVideoError("clip duration exceeds provider max_video_seconds")
        if cancel_event is not None and cancel_event.is_set():
            self._upsert_record(clip, state="failed", note="cancelled")
            raise RequestCancelled(f"upload {clip.upload_id} cancelled")
        backend = self.backend
        assert backend is not None
        self._upsert_record(clip, state="planned")
        handle: NativeVideoHandle | None = None
        usage: Usage | None = None
        try:
            handle = backend.upload(clip, cancel_event=cancel_event)
            self._upsert_record(
                clip,
                state="uploaded",
                remote=RedactedRemoteHandle(
                    provider=handle.provider,
                    handle_digest=handle.handle_digest,
                    state="uploaded",
                ),
            )
            handle = backend.wait_ready(handle, cancel_event=cancel_event)
            self._upsert_record(
                clip,
                state="ready",
                remote=RedactedRemoteHandle(
                    provider=handle.provider,
                    handle_digest=handle.handle_digest,
                    state="ready",
                ),
            )
            if cancel_event is not None and cancel_event.is_set():
                raise RequestCancelled(f"analyze {clip.upload_id} cancelled")
            analysis = backend.analyze(
                handle,
                prompt_digest=prompt_digest,
                cancel_event=cancel_event,
            )
            usage = analysis.usage
            units = analysis.structured.get("units") if isinstance(analysis.structured, dict) else None
            if not units:
                raise NativeVideoEmptyOutput("native provider returned no knowledge units")
            self._upsert_record(
                clip,
                state="analyzed",
                remote=RedactedRemoteHandle(
                    provider=handle.provider,
                    handle_digest=handle.handle_digest,
                    state="analyzed",
                ),
                usage=usage,
            )
            if delete_after:
                self._attempt_delete(backend, handle, clip, cancel_event=cancel_event, usage=usage)
            elif not self.capabilities.can_delete_remote:
                self._upsert_record(
                    clip,
                    state="retained",
                    remote=RedactedRemoteHandle(
                        provider=handle.provider,
                        handle_digest=handle.handle_digest,
                        state="retained",
                    ),
                    usage=usage,
                    note="delete_after=False; remote retained",
                )
            return analysis
        except RequestCancelled:
            self._upsert_record(clip, state="failed", note="cancelled", usage=usage)
            if handle is not None:
                self._attempt_delete(
                    backend,
                    handle,
                    clip,
                    cancel_event=cancel_event,
                    usage=usage,
                    keep_state="failed",
                )
            raise
        except NativeVideoError as error:
            self._upsert_record(clip, state="failed", note=str(error)[:400], usage=usage)
            if handle is not None:
                self._attempt_delete(
                    backend,
                    handle,
                    clip,
                    cancel_event=cancel_event,
                    usage=usage,
                    keep_state="failed",
                )
            raise


@dataclass
class FakeNativeVideoBackend(NativeVideoBackend):
    """Deterministic backend for contract tests — no vendor credentials."""

    provider: str = "fake-native"
    raw_handle_prefix: str = "fh"
    fail_upload: bool = False
    fail_ready: bool = False
    fail_analyze: bool = False
    fail_delete: bool = False
    cancel_on_upload: bool = False
    structured: dict[str, Any] | None = None
    approximate_seconds: list[float] = field(default_factory=list)
    uploads: list[ClipUpload] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    def upload(
        self,
        clip: ClipUpload,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        if self.cancel_on_upload or (cancel_event is not None and cancel_event.is_set()):
            raise RequestCancelled(f"upload {clip.upload_id} cancelled")
        if self.fail_upload:
            raise NativeVideoError("simulated upload failure")
        self.uploads.append(clip)
        raw = f"{self.raw_handle_prefix}-{clip.upload_id}"
        return NativeVideoHandle(
            upload_id=clip.upload_id,
            provider=self.provider,
            handle_digest=redact_handle(self.provider, raw),
            state="uploading",
            source_range=clip.source_range,
        )

    def wait_ready(
        self,
        handle: NativeVideoHandle,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"ready {handle.upload_id} cancelled")
        if self.fail_ready:
            raise NativeVideoError("simulated readiness failure")
        return handle.model_copy(update={"state": "ready"})

    def analyze(
        self,
        handle: NativeVideoHandle,
        *,
        prompt_digest: Digest,
        cancel_event: Event | None = None,
    ) -> NativeVideoAnalysis:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"analyze {handle.upload_id} cancelled")
        if self.fail_analyze:
            raise NativeVideoError("simulated analyze failure")
        structured = self.structured
        if structured is None:
            structured = {
                "units": [
                    {
                        "id": f"unit-{handle.upload_id}",
                        "topic_id": "topic-native",
                        "segment_ids": [handle.source_range.segment_id or "seg-native"],
                        "start_seconds": handle.source_range.start_seconds,
                        "end_seconds": handle.source_range.end_seconds,
                        "kind": "procedure",
                        "claims": [
                            {
                                "id": f"claim-{handle.upload_id}",
                                "text": "native-supported step",
                                "evidence_ids": ["cap-001"],
                                "status": "supported",
                                "qualifiers": [],
                                "modality": "visual",
                                "provenance": "source",
                            }
                        ],
                        "relations": [],
                        "visual_candidates": [],
                        "uncertainty": [],
                        "evidence_requests": [],
                    }
                ],
                "prompt_digest": prompt_digest,
            }
        usage = Usage(
            request_id=f"native:{handle.upload_id}",
            input_tokens=120,
            output_tokens=40,
            video_seconds=max(
                0.0,
                handle.source_range.end_seconds - handle.source_range.start_seconds,
            ),
            estimated_usd=0.01,
        )
        stamps = [
            ApproximateTimestamp(seconds=value, confidence=0.7, note="fake probe")
            for value in self.approximate_seconds
        ]
        return NativeVideoAnalysis(
            upload_id=handle.upload_id,
            structured=structured,
            usage=usage,
            approximate_timestamps=stamps,
        )

    def delete_remote(
        self,
        handle: NativeVideoHandle,
        *,
        cancel_event: Event | None = None,
    ) -> NativeVideoHandle:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"delete {handle.upload_id} cancelled")
        if self.fail_delete:
            raise DeleteRemoteFailed("simulated delete failure")
        self.deleted.append(handle.upload_id)
        return handle.model_copy(update={"state": "deleted"})


def fake_native_adapter(
    *,
    capabilities: ProviderCapabilities | None = None,
    backend: FakeNativeVideoBackend | None = None,
) -> NativeVideoAdapter:
    caps = capabilities or ProviderCapabilities(
        supports_images=True,
        supports_video=True,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=32000,
        max_output_tokens=4000,
        max_images=0,
        max_video_seconds=120.0,
        remote_retention="ephemeral",
        can_delete_remote=True,
    )
    return NativeVideoAdapter(
        provider_name="fake-native",
        capabilities=caps,
        backend=backend or FakeNativeVideoBackend(),
    )
