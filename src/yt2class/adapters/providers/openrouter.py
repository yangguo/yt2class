"""OpenRouter chat/completions adapter for M2–M3 stage payloads."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from threading import Event
from typing import Any

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
from yt2class.orchestration.retry import NonRetryableError, RetryableError, parse_retry_after
from yt2class.stages.llm_util import payload_digest

DEFAULT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_REFERER = "https://github.com/yangguo/yt2class"
DEFAULT_APP_TITLE = "yt2class"


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


class OpenRouterProvider(Provider):
    """HTTP vision provider; stages attach JSON payloads on ``last_payload``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = 180.0,
        capabilities: ProviderCapabilities | None = None,
        client: httpx.Client | None = None,
        referer: str | None = None,
        app_title: str | None = None,
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
        text = (
            f"{prompt}\n\n"
            "Respond with a single JSON object only. No markdown fences or commentary.\n\n"
            f"Request role: {request.role}\n"
            f"Payload:\n{serialized}"
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

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = "OpenRouter request failed"
        try:
            body = response.json()
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict) and error.get("message"):
                    detail = str(error["message"])
                elif isinstance(error, str):
                    detail = error
        except ValueError:
            detail = response.text[:240] or detail
        if response.status_code in {401, 403}:
            raise NonRetryableError(detail)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableError(
                detail,
                status_code=response.status_code,
                retry_after=parse_retry_after(response.headers.get("Retry-After")),
            )
        raise NonRetryableError(detail)

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        client = self._client or httpx.Client(timeout=self._timeout)
        owns_client = self._client is None
        try:
            response = client.post(self._endpoint, headers=self._headers(), json=body)
            self._raise_for_status(response)
            data = response.json()
        except httpx.HTTPError as error:
            raise RetryableError(f"OpenRouter transport error: {error.__class__.__name__}") from error
        finally:
            if owns_client:
                client.close()
        if not isinstance(data, dict):
            raise ProviderError("OpenRouter response was not a JSON object")
        return data

    def _complete(
        self,
        request: ModelRequest,
        *,
        cancel_event: Event | None = None,
    ) -> ModelResult:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelled(f"request {request.request_id} cancelled")
        payload = self.last_payload
        if not isinstance(payload, dict):
            raise ProviderError("OpenRouter provider missing stage payload (last_payload)")
        if payload_digest(payload) != request.payload_digest:
            raise ProviderError("stage payload digest does not match ModelRequest")
        body = {
            "model": self._model,
            "temperature": 0.1,
            "messages": self._build_messages(request, payload),
            "response_format": {"type": "json_object"},
        }
        data = self._post(body)
        text = self._extract_message_content(data)
        try:
            structured = parse_model_json(text)
        except Exception as error:
            raise MissingStructuredOutput(
                f"OpenRouter returned invalid JSON for {request.request_id}: {error}"
            ) from error
        return ModelResult(
            request_id=request.request_id,
            structured=structured,
            usage=self._usage_from_response(request, data),
        )


__all__ = [
    "DEFAULT_MODEL",
    "OpenRouterProvider",
    "openrouter_capabilities",
    "resolve_openrouter_endpoint",
    "resolve_openrouter_model",
]
