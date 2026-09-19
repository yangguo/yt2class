"""OpenRouter chat/completions adapter for M2–M3 stage payloads."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from threading import Event
from typing import Any, Literal

import httpx

from yt2class.adapters.providers.base import (
    MissingStructuredOutput,
    ModelRequest,
    ModelResult,
    Provider,
    ProviderCapabilities,
    ProviderError,
    RequestCancelled,
    Usage,
)
from yt2class.config import AnalysisConfig
from yt2class.domain.visual import VisualCatalogue
from yt2class.llm import parse_model_json
from yt2class.orchestration.retry import (
    InvalidJsonResponse,
    NonRetryableError,
    RetryableError,
    parse_retry_after,
)

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 300.0

from yt2class.stages.llm_util import payload_digest, thread_local_provider_payload
from yt2class.stages.structured_coerce import ROLE_JSON_REMINDERS

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_REFERER = "https://github.com/yangguo/yt2class"
DEFAULT_APP_TITLE = "yt2class"

OpenRouterJsonMode = Literal["auto", "on", "off"]
STRUCTURED_OUTPUT_REJECT_HINTS = (
    "structured-output",
    "structured output",
    "structured-outputs",
    "json_object",
    "response_format",
    "json schema",
)


def openrouter_capabilities() -> ProviderCapabilities:
    return ProviderCapabilities(
        supports_images=True,
        supports_video=False,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=128_000,
        max_output_tokens=8_192,
        max_images=8,
        max_video_seconds=0.0,
    )


def resolve_openrouter_model(analysis: AnalysisConfig) -> str:
    if getattr(analysis, "model", None):
        model = analysis.model
        if model:
            return model
    for key in ("YT2CLASS_OPENROUTER_MODEL", "OPENROUTER_MODEL"):
        value = os.getenv(key)
        if value:
            return value
    return DEFAULT_MODEL


def resolve_openrouter_endpoint() -> str:
    explicit = os.getenv("OPENROUTER_BASE_URL") or os.getenv("YT2CLASS_OPENROUTER_BASE_URL")
    if explicit:
        base = explicit.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"
    return DEFAULT_ENDPOINT


def _normalize_openrouter_json_mode(raw: str) -> OpenRouterJsonMode:
    value = raw.strip().lower()
    if value in {"auto", "on", "off", "true", "false", "1", "0", "yes", "no"}:
        if value in {"true", "1", "yes", "on"}:
            return "on"
        if value in {"false", "0", "no", "off"}:
            return "off"
        return value  # type: ignore[return-value]
    raise ProviderError(
        f"invalid OpenRouter JSON mode {raw!r}; expected auto, on, or off "
        "(config analysis.openrouter_json_mode or env OPENROUTER_JSON_MODE)"
    )


def resolve_openrouter_timeout_seconds(analysis: AnalysisConfig) -> float:
    for key in ("YT2CLASS_OPENROUTER_TIMEOUT_SECONDS", "OPENROUTER_TIMEOUT_SECONDS"):
        value = os.getenv(key)
        if value:
            try:
                parsed = float(value)
            except ValueError as error:
                raise ProviderError(f"invalid {key}={value!r}") from error
            if parsed <= 0:
                raise ProviderError(f"{key} must be positive")
            return parsed
    configured = float(getattr(analysis, "openrouter_timeout_seconds", DEFAULT_TIMEOUT_SECONDS))
    return configured if configured > 0 else DEFAULT_TIMEOUT_SECONDS


def resolve_openrouter_json_mode(analysis: AnalysisConfig) -> OpenRouterJsonMode:
    for key in ("YT2CLASS_OPENROUTER_JSON_MODE", "OPENROUTER_JSON_MODE"):
        value = os.getenv(key)
        if value:
            return _normalize_openrouter_json_mode(value)
    configured = getattr(analysis, "openrouter_json_mode", None)
    if configured:
        return _normalize_openrouter_json_mode(str(configured))
    return "auto"


def structured_output_rejected(status_code: int, detail: str) -> bool:
    if status_code != 400:
        return False
    lowered = detail.lower()
    return any(hint in lowered for hint in STRUCTURED_OUTPUT_REJECT_HINTS)


class StructuredOutputUnsupported(ProviderError):
    """Upstream rejected response_format / json_object (retry without it in auto mode)."""

    code = "structured_output_unsupported"


class OpenRouterProvider(Provider):
    """HTTP vision provider; stages attach JSON payloads on ``last_payload``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        capabilities: ProviderCapabilities | None = None,
        client: httpx.Client | None = None,
        referer: str | None = None,
        app_title: str | None = None,
        json_mode: OpenRouterJsonMode = "auto",
    ) -> None:
        super().__init__(capabilities or openrouter_capabilities())
        if not api_key.strip():
            raise ProviderError("OPENROUTER_API_KEY is required for analysis.provider openrouter")
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._client = client
        self._referer = referer or os.getenv("OPENROUTER_HTTP_REFERER", DEFAULT_REFERER)
        self._app_title = app_title or os.getenv("OPENROUTER_APP_TITLE", DEFAULT_APP_TITLE)
        self._json_mode = json_mode
        self.last_payload: dict[str, Any] | None = None
        self._run_root: Path | None = None
        self._visual: VisualCatalogue | None = None

    @classmethod
    def from_config(cls, analysis: AnalysisConfig, *, client: httpx.Client | None = None) -> OpenRouterProvider:
        api_key = os.getenv("OPENROUTER_API_KEY", "")
        return cls(
            api_key=api_key,
            model=resolve_openrouter_model(analysis),
            endpoint=resolve_openrouter_endpoint(),
            client=client,
            json_mode=resolve_openrouter_json_mode(analysis),
            timeout_seconds=resolve_openrouter_timeout_seconds(analysis),
        )

    def bind_run_context(self, run_root: Path, *, visual: VisualCatalogue | None = None) -> None:
        self._run_root = run_root.expanduser().resolve()
        if visual is not None:
            self._visual = visual
            return
        visual_path = self._run_root / "evidence" / "visual.json"
        if visual_path.is_file():
            self._visual = VisualCatalogue.model_validate_json(visual_path.read_text(encoding="utf-8"))

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": self._referer,
            "X-Title": self._app_title,
        }

    def _visual_catalogue(self) -> VisualCatalogue | None:
        if self._visual is not None:
            return self._visual
        if self._run_root is None:
            return None
        visual_path = self._run_root / "evidence" / "visual.json"
        if not visual_path.is_file():
            return None
        self._visual = VisualCatalogue.model_validate_json(visual_path.read_text(encoding="utf-8"))
        return self._visual

    @staticmethod
    def _data_url(path: Path) -> str:
        mime, _ = mimetypes.guess_type(path.name)
        if not mime or not mime.startswith("image/"):
            mime = "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    def _image_paths_for_payload(self, payload: dict[str, Any], limit: int) -> list[Path]:
        if limit <= 0 or self._run_root is None:
            return []
        visual = self._visual_catalogue()
        if visual is None:
            return []
        assets = {asset.id: asset for asset in visual.assets}
        occurrences = {occurrence.id: occurrence for occurrence in visual.occurrences}
        paths: list[Path] = []
        for frame in payload.get("frames") or []:
            if len(paths) >= limit:
                break
            frame_id = frame.get("id")
            if not isinstance(frame_id, str):
                continue
            occurrence = occurrences.get(frame_id)
            if occurrence is None:
                continue
            asset = assets.get(occurrence.asset_id)
            if asset is None:
                continue
            candidate = (self._run_root / asset.path).resolve()
            if candidate.is_file():
                paths.append(candidate)
        return paths

    def _build_messages(self, request: ModelRequest, payload: dict[str, Any]) -> list[dict[str, Any]]:
        prompt = str(payload.get("prompt") or "Follow the JSON schema implied by your role.")
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        reminder = ROLE_JSON_REMINDERS.get(request.role, "Return only the specified JSON object.")
        text = (
            f"{prompt}\n\n"
            "Respond with a single JSON object only. No markdown fences or commentary.\n\n"
            f"Request role: {request.role}\n"
            f"Payload:\n{serialized}\n\n"
            f"{reminder}"
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for path in self._image_paths_for_payload(payload, request.image_count):
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": self._data_url(path)},
                }
            )
        return [{"role": "user", "content": content}]

    @staticmethod
    def _extract_message_content(data: dict[str, Any]) -> str:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("OpenRouter response missing choices")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("OpenRouter response missing message")
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            joined = "".join(parts)
            if joined:
                return joined
        raise ProviderError("OpenRouter response did not contain text content")

    @staticmethod
    def _usage_from_response(request: ModelRequest, data: dict[str, Any]) -> Usage:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return Usage(
                request_id=request.request_id,
                input_tokens=0,
                output_tokens=0,
                image_count=request.image_count,
                video_seconds=request.video_seconds,
            )
        prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        return Usage(
            request_id=request.request_id,
            input_tokens=max(0, prompt_tokens),
            output_tokens=max(0, completion_tokens),
            image_count=request.image_count,
            video_seconds=request.video_seconds,
        )

    @staticmethod
    def _error_detail(body: dict[str, Any]) -> str:
        error = body.get("error")
        if isinstance(error, str) and error.strip():
            return error.strip()
        if not isinstance(error, dict):
            return "OpenRouter request failed"
        parts: list[str] = []
        message = error.get("message")
        if message:
            parts.append(str(message))
        metadata = error.get("metadata")
        if isinstance(metadata, dict):
            raw = metadata.get("raw")
            if raw is not None and str(raw).strip():
                parts.append(f"metadata.raw={raw}")
            provider_name = metadata.get("provider_name") or metadata.get("provider")
            if provider_name:
                parts.append(f"provider={provider_name}")
        code = error.get("code")
        if code is not None:
            parts.append(f"code={code}")
        return "; ".join(parts) if parts else "OpenRouter request failed"

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = "OpenRouter request failed"
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = self._error_detail(body)
        except ValueError:
            detail = response.text[:240] or detail
        if response.status_code in {401, 403}:
            raise NonRetryableError(detail)
        if response.status_code == 400 and structured_output_rejected(response.status_code, detail):
            raise StructuredOutputUnsupported(detail)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(
                detail,
                status_code=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        raise NonRetryableError(detail)

    def _build_request_body(
        self,
        request: ModelRequest,
        payload: dict[str, Any],
        *,
        use_structured_output: bool,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": 0.1,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "messages": self._build_messages(request, payload),
        }
        if use_structured_output:
            body["response_format"] = {"type": "json_object"}
        return body

    def _httpx_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(
            connect=min(30.0, self._timeout),
            read=self._timeout,
            write=min(60.0, self._timeout),
            pool=min(30.0, self._timeout),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=self._httpx_timeout())
        owns_client = self._client is None
        try:
            response = client.post(self._endpoint, headers=self._headers(), json=body)
            self._raise_for_status(response)
            data = response.json()
        except httpx.ReadTimeout as error:
            raise RetryableError(
                f"OpenRouter read timed out after {self._timeout:.0f}s"
            ) from error
        except httpx.ConnectTimeout as error:
            raise RetryableError(
                f"OpenRouter connect timed out after {min(30.0, self._timeout):.0f}s"
            ) from error
        except httpx.TimeoutException as error:
            raise RetryableError(f"OpenRouter request timed out: {error}") from error
        except httpx.HTTPError as error:
            raise RetryableError(f"OpenRouter transport error: {error.__class__.__name__}") from error
        finally:
            if owns_client:
                client.close()
        if not isinstance(data, dict):
            raise ProviderError("OpenRouter response was not a JSON object")
        return data

    def _complete_with_json_mode(
        self,
        request: ModelRequest,
        payload: dict[str, Any],
        *,
        use_structured_output: bool,
    ) -> ModelResult:
        body = self._build_request_body(
            request,
            payload,
            use_structured_output=use_structured_output,
        )
        data = self._post(body)
        text = self._extract_message_content(data)
        try:
            structured = parse_model_json(text)
        except Exception as error:
            message = str(error)
            if "Unterminated" in message or "Expecting value" in message:
                raise InvalidJsonResponse(
                    f"OpenRouter returned truncated JSON for {request.request_id}: {error}"
                ) from error
            raise MissingStructuredOutput(
                f"OpenRouter returned invalid JSON for {request.request_id}: {error}"
            ) from error
        return ModelResult(
            request_id=request.request_id,
            structured=structured,
            usage=self._usage_from_response(request, data),
        )

    def _complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"request {request.request_id} cancelled")
        thread_payload = thread_local_provider_payload()
        payload = (
            thread_payload
            if isinstance(thread_payload, dict)
            and payload_digest(thread_payload) == request.payload_digest
            else self.last_payload
        )
        if not isinstance(payload, dict):
            raise ProviderError("OpenRouter provider missing stage payload (last_payload)")
        if payload_digest(payload) != request.payload_digest:
            raise ProviderError("stage payload digest does not match ModelRequest")
        if self._json_mode == "off":
            return self._complete_with_json_mode(
                request,
                payload,
                use_structured_output=False,
            )
        if self._json_mode == "on":
            return self._complete_with_json_mode(
                request,
                payload,
                use_structured_output=True,
            )
        try:
            return self._complete_with_json_mode(
                request,
                payload,
                use_structured_output=True,
            )
        except StructuredOutputUnsupported:
            return self._complete_with_json_mode(
                request,
                payload,
                use_structured_output=False,
            )


__all__ = [
    "DEFAULT_MODEL",
    "OpenRouterJsonMode",
    "OpenRouterProvider",
    "StructuredOutputUnsupported",
    "openrouter_capabilities",
    "resolve_openrouter_endpoint",
    "resolve_openrouter_json_mode",
    "resolve_openrouter_model",
    "resolve_openrouter_timeout_seconds",
    "structured_output_rejected",
]
