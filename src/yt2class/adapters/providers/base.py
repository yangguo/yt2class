"""Provider capability and request/result contracts. No stage orchestration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import hashlib
import json
from threading import Event
from typing import Any, Literal

from pydantic import Field, model_validator

from yt2class.domain.common import (
    Digest,
    Identifier,
    Seconds,
    StrictModel,
    validate_half_open,
)

Modality = Literal["text", "image", "audio", "video"]
ProviderRole = Literal["outline", "segment", "editor", "verifier"]
DesiredModality = Literal["frame", "clip", "audio"]
RemoteRetention = Literal["none", "ephemeral", "unknown"]


class ProviderError(RuntimeError):
    code: str = "provider_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class UnsupportedModality(ProviderError):
    code = "unsupported_modality"


class ContextOverflow(ProviderError):
    code = "context_overflow"


class MissingStructuredOutput(ProviderError):
    code = "missing_structured_output"


class RequestCancelled(ProviderError):
    code = "cancelled"


class RequestTimeout(ProviderError):
    code = "timeout"


class MissingUsage(ProviderError):
    code = "missing_usage"


class RequestFingerprintConflict(ProviderError):
    """The request ID was reused for a materially different request."""

    code = "idempotency_conflict"


class ProviderCapabilities(StrictModel):
    """Declared by configuration and smoke tests, not inferred from endpoint names."""

    supports_images: bool
    supports_video: bool
    supports_audio: bool
    supports_structured_output: bool
    reports_usage: bool
    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    max_images: int = Field(ge=0)
    max_video_seconds: float = Field(ge=0, allow_inf_nan=False)
    remote_retention: RemoteRetention = "none"
    can_delete_remote: bool = False


class Usage(StrictModel):
    request_id: Identifier
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    image_count: int = Field(default=0, ge=0)
    video_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class EvidenceRequest(StrictModel):
    start_seconds: Seconds
    end_seconds: Seconds
    reason: str = Field(min_length=1, max_length=240)
    desired_modality: DesiredModality

    @model_validator(mode="after")
    def check_range(self) -> EvidenceRequest:
        validate_half_open(self.start_seconds, self.end_seconds, label="evidence request")
        return self


class ModelRequest(StrictModel):
    request_id: Identifier
    role: ProviderRole
    modalities: list[Modality] = Field(min_length=1)
    estimated_input_tokens: int = Field(ge=0)
    estimated_output_tokens: int = Field(ge=0)
    image_count: int = Field(default=0, ge=0)
    video_seconds: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    require_structured_output: bool = True
    payload_digest: Digest


class ModelResult(StrictModel):
    request_id: Identifier
    structured: dict[str, Any] | None = None
    usage: Usage
    evidence_requests: list[EvidenceRequest] = Field(default_factory=list)


class Provider(ABC):
    """Shared contract checks. Concrete adapters implement ``_complete`` only."""

    def __init__(self, capabilities: ProviderCapabilities) -> None:
        self.capabilities = capabilities
        self._completed: dict[str, tuple[str, ModelResult]] = {}

    @staticmethod
    def request_fingerprint(request: ModelRequest) -> str:
        """Return a stable digest of every request field except its id.

        ``request_id`` provides the idempotency slot.  The fingerprint makes
        reusing that slot safe when a retry accidentally changes the role,
        payload, modality, or token/payload budget.
        """

        payload = request.model_dump(mode="json", exclude={"request_id"})
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def check_request(self, request: ModelRequest) -> None:
        caps = self.capabilities
        if "image" in request.modalities and not caps.supports_images:
            raise UnsupportedModality("provider does not support image modality")
        if "audio" in request.modalities and not caps.supports_audio:
            raise UnsupportedModality("provider does not support audio modality")
        if "video" in request.modalities and not caps.supports_video:
            raise UnsupportedModality("provider does not support video modality")
        if request.image_count > 0 and not caps.supports_images:
            raise UnsupportedModality("provider does not support image payloads")
        if request.video_seconds > 0 and not caps.supports_video:
            raise UnsupportedModality("provider does not support video payloads")
        if request.require_structured_output and not caps.supports_structured_output:
            raise MissingStructuredOutput("provider does not support structured output")
        if (
            request.estimated_input_tokens > caps.max_input_tokens
            or request.estimated_output_tokens > caps.max_output_tokens
            or request.image_count > caps.max_images
            or request.video_seconds > caps.max_video_seconds
        ):
            raise ContextOverflow("request exceeds provider context or payload limits")

    def ensure_result(self, request: ModelRequest, result: ModelResult | None) -> ModelResult:
        if result is None or result.usage is None:
            raise MissingUsage("provider result missing usage")
        if result.request_id != request.request_id:
            raise ProviderError("provider result request_id does not match the request")
        if result.usage.request_id != request.request_id:
            raise ProviderError("usage request_id does not match the request")
        if not self.capabilities.reports_usage and (
            result.usage.input_tokens or result.usage.output_tokens
        ):
            # Still accept explicit zeros; nonzero usage without reporting is fine if present.
            pass
        if request.require_structured_output and result.structured is None:
            raise MissingStructuredOutput("provider result missing structured output")
        return result

    def complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        self.check_request(request)
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"request {request.request_id} cancelled")
        fingerprint = self.request_fingerprint(request)
        cached = self._completed.get(request.request_id)
        if cached is not None:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint != fingerprint:
                raise RequestFingerprintConflict(
                    f"request_id {request.request_id!r} was already completed "
                    "with a different request fingerprint"
                )
            return cached_result
        result = self.ensure_result(request, self._complete(request, cancel_event=cancel_event))
        self._completed[request.request_id] = (fingerprint, result)
        return result

    @abstractmethod
    def _complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        raise NotImplementedError


class FakeProvider(Provider):
    """Deterministic provider for contract tests and the M2 fake analysis path.

    Not a live model. Stages may attach the hashed payload on ``last_payload``
    so a responder can emit structurally valid CourseMap / KnowledgeUnit JSON.
    """

    def __init__(
        self,
        capabilities: ProviderCapabilities,
        *,
        structured: dict[str, Any] | None = None,
        omit_structured: bool = False,
        omit_usage: bool = False,
        timeout: bool = False,
        cancel: bool = False,
        responder: Any | None = None,
        sequential: list[Any] | None = None,
    ) -> None:
        super().__init__(capabilities)
        self._structured = None if omit_structured else (structured or {"ok": True})
        self._omit_usage = omit_usage
        self._timeout = timeout
        self._cancel = cancel
        self._responder = responder
        self._sequential = list(sequential or [])
        self._seq_index = 0
        self.requests: list[ModelRequest] = []
        self.last_payload: dict[str, Any] | None = None

    def _next_structured(self, request: ModelRequest) -> dict[str, Any] | None:
        if self._sequential:
            if self._seq_index >= len(self._sequential):
                item = self._sequential[-1]
            else:
                item = self._sequential[self._seq_index]
            self._seq_index += 1
            if isinstance(item, BaseException):
                raise item
            return item
        if self._responder is not None:
            return self._responder(self, request)
        return self._structured

    def _complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        self.requests.append(request)
        if self._timeout:
            raise RequestTimeout(f"request {request.request_id} timed out")
        if self._cancel or (cancel_event is not None and cancel_event.is_set()):
            raise RequestCancelled(f"request {request.request_id} cancelled")
        if self._omit_usage:
            return None  # type: ignore[return-value]
        return ModelResult(
            request_id=request.request_id,
            structured=self._next_structured(request),
            usage=Usage(
                request_id=request.request_id,
                input_tokens=request.estimated_input_tokens,
                output_tokens=1,
                image_count=request.image_count,
                video_seconds=request.video_seconds,
            ),
        )
