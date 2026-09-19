"""Volcengine Ark Agent Plan chat/completions adapter (vision-capable)."""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
from pathlib import Path
from threading import Event
from typing import Any, Literal
from urllib.parse import urlparse

import httpx

from yt2class.adapters.http_deadline import post_json_with_wall_clock_deadline
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
from yt2class.adapters.providers.openrouter import (
    DEFAULT_TIMEOUT_SECONDS,
    StructuredOutputUnsupported,
    structured_output_rejected,
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
from yt2class.stages.llm_util import payload_digest, thread_local_provider_payload
from yt2class.stages.structured_coerce import ROLE_JSON_REMINDERS

DEFAULT_ENDPOINT = "https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions"
DEFAULT_MODEL = "ark-code-latest"
DEFAULT_ARK_MAX_IMAGES_PER_BATCH = 4
DEFAULT_ARK_OUTLINE_MAX_VISUAL_OVERVIEW = 8
DEFAULT_ARK_SEGMENT_MAX_OCR_REGIONS = 48
DEFAULT_ARK_SEGMENT_MAX_EVIDENCE_IDS = 160
DEFAULT_ARK_SEGMENT_OCR_TEXT_CHARS = 240
DEFAULT_ARK_MAX_TOKENS = 16_384
DEFAULT_ARK_MAX_TOKENS_LENGTH_RETRY = 32_768
DEFAULT_ARK_REASONING_EFFORT = "none"
_LENGTH_RETRY_JSON_REMINDER = (
    "Your previous answer was truncated before any JSON object was returned. "
    "Put the full JSON object in message.content only. No prose, markdown, or reasoning."
)

ArkJsonMode = Literal["auto", "on", "off"]
ArkReasoningEffort = Literal["none", "low", "medium", "high"]

logger = logging.getLogger(__name__)


def volcengine_ark_capabilities(
    *,
    max_images: int = DEFAULT_ARK_MAX_IMAGES_PER_BATCH,
    max_output_tokens: int = DEFAULT_ARK_MAX_TOKENS_LENGTH_RETRY,
) -> ProviderCapabilities:
    return ProviderCapabilities(
        supports_images=True,
        supports_video=False,
        supports_audio=False,
        supports_structured_output=True,
        reports_usage=True,
        max_input_tokens=128_000,
        max_output_tokens=max(1024, max_output_tokens),
        max_images=max(0, max_images),
        max_video_seconds=0.0,
    )


def resolve_volcengine_ark_api_key() -> str:
    for key in ("VOLCENGINE_ARK_API_KEY", "ARK_API_KEY", "YT2CLASS_VOLCENGINE_ARK_API_KEY"):
        value = os.getenv(key)
        if value:
            return value
    return ""


def resolve_volcengine_ark_model(analysis: AnalysisConfig) -> str:
    if getattr(analysis, "model", None):
        model = analysis.model
        if model:
            return model
    for key in ("YT2CLASS_ARK_MODEL", "ARK_MODEL", "VOLCENGINE_ARK_MODEL"):
        value = os.getenv(key)
        if value:
            return value
    return DEFAULT_MODEL


def resolve_volcengine_ark_endpoint() -> str:
    explicit = (
        os.getenv("VOLCENGINE_ARK_BASE_URL")
        or os.getenv("ARK_BASE_URL")
        or os.getenv("YT2CLASS_VOLCENGINE_ARK_BASE_URL")
    )
    if explicit:
        base = explicit.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"
    return DEFAULT_ENDPOINT


def _normalize_ark_json_mode(raw: str) -> ArkJsonMode:
    value = raw.strip().lower()
    if value in {"auto", "on", "off", "true", "false", "1", "0", "yes", "no"}:
        if value in {"true", "1", "yes", "on"}:
            return "on"
        if value in {"false", "0", "no", "off"}:
            return "off"
        return value  # type: ignore[return-value]
    raise ProviderError(
        f"invalid Ark JSON mode {raw!r}; expected auto, on, or off "
        "(config analysis.ark_json_mode, analysis.openrouter_json_mode, or env ARK_JSON_MODE)"
    )


def resolve_volcengine_ark_json_mode(analysis: AnalysisConfig) -> ArkJsonMode:
    for key in ("YT2CLASS_ARK_JSON_MODE", "ARK_JSON_MODE", "VOLCENGINE_ARK_JSON_MODE"):
        value = os.getenv(key)
        if value:
            return _normalize_ark_json_mode(value)
    configured = getattr(analysis, "ark_json_mode", None)
    if configured:
        return _normalize_ark_json_mode(str(configured))
    return resolve_openrouter_json_mode_fallback(analysis)


def resolve_openrouter_json_mode_fallback(analysis: AnalysisConfig) -> ArkJsonMode:
    from yt2class.adapters.providers.openrouter import resolve_openrouter_json_mode

    return resolve_openrouter_json_mode(analysis)


def resolve_volcengine_ark_max_images_per_batch(analysis: AnalysisConfig) -> int:
    for key in (
        "YT2CLASS_ARK_MAX_IMAGES_PER_BATCH",
        "ARK_MAX_IMAGES_PER_BATCH",
        "VOLCENGINE_ARK_MAX_IMAGES_PER_BATCH",
    ):
        value = os.getenv(key)
        if value:
            try:
                parsed = int(value)
            except ValueError as error:
                raise ProviderError(f"invalid {key}={value!r}") from error
            if parsed <= 0:
                raise ProviderError(f"{key} must be positive")
            return min(analysis.max_images_per_batch, parsed)
    configured = getattr(analysis, "ark_max_images_per_batch", None)
    if configured is not None and int(configured) > 0:
        return min(analysis.max_images_per_batch, int(configured))
    return min(analysis.max_images_per_batch, DEFAULT_ARK_MAX_IMAGES_PER_BATCH)


def _resolve_positive_int_env(
    keys: tuple[str, ...],
    *,
    configured: int | None,
    default: int,
) -> int:
    for key in keys:
        value = os.getenv(key)
        if value:
            try:
                parsed = int(value)
            except ValueError as error:
                raise ProviderError(f"invalid {key}={value!r}") from error
            if parsed <= 0:
                raise ProviderError(f"{key} must be positive")
            return parsed
    if configured is not None and int(configured) > 0:
        return int(configured)
    return default


def resolve_volcengine_ark_segment_max_ocr_regions(analysis: AnalysisConfig) -> int:
    return _resolve_positive_int_env(
        (
            "YT2CLASS_ARK_SEGMENT_MAX_OCR_REGIONS",
            "ARK_SEGMENT_MAX_OCR_REGIONS",
            "VOLCENGINE_ARK_SEGMENT_MAX_OCR_REGIONS",
        ),
        configured=getattr(analysis, "ark_segment_max_ocr_regions", None),
        default=DEFAULT_ARK_SEGMENT_MAX_OCR_REGIONS,
    )


def resolve_volcengine_ark_segment_max_evidence_ids(analysis: AnalysisConfig) -> int:
    return _resolve_positive_int_env(
        (
            "YT2CLASS_ARK_SEGMENT_MAX_EVIDENCE_IDS",
            "ARK_SEGMENT_MAX_EVIDENCE_IDS",
            "VOLCENGINE_ARK_SEGMENT_MAX_EVIDENCE_IDS",
        ),
        configured=getattr(analysis, "ark_segment_max_evidence_ids", None),
        default=DEFAULT_ARK_SEGMENT_MAX_EVIDENCE_IDS,
    )


def resolve_volcengine_ark_segment_ocr_text_chars(analysis: AnalysisConfig) -> int:
    return _resolve_positive_int_env(
        (
            "YT2CLASS_ARK_SEGMENT_OCR_TEXT_CHARS",
            "ARK_SEGMENT_OCR_TEXT_CHARS",
            "VOLCENGINE_ARK_SEGMENT_OCR_TEXT_CHARS",
        ),
        configured=getattr(analysis, "ark_segment_ocr_text_chars", None),
        default=DEFAULT_ARK_SEGMENT_OCR_TEXT_CHARS,
    )


def resolve_volcengine_ark_outline_max_visual_overview(analysis: AnalysisConfig) -> int:
    for key in (
        "YT2CLASS_ARK_OUTLINE_MAX_VISUAL_OVERVIEW",
        "ARK_OUTLINE_MAX_VISUAL_OVERVIEW",
        "VOLCENGINE_ARK_OUTLINE_MAX_VISUAL_OVERVIEW",
    ):
        value = os.getenv(key)
        if value:
            try:
                parsed = int(value)
            except ValueError as error:
                raise ProviderError(f"invalid {key}={value!r}") from error
            if parsed <= 0:
                raise ProviderError(f"{key} must be positive")
            return parsed
    configured = getattr(analysis, "ark_outline_max_visual_overview", None)
    if configured is not None and int(configured) > 0:
        return int(configured)
    return DEFAULT_ARK_OUTLINE_MAX_VISUAL_OVERVIEW


def resolve_volcengine_ark_max_tokens(analysis: AnalysisConfig) -> int:
    return _resolve_positive_int_env(
        ("YT2CLASS_ARK_MAX_TOKENS", "ARK_MAX_TOKENS", "VOLCENGINE_ARK_MAX_TOKENS"),
        configured=getattr(analysis, "ark_max_tokens", None),
        default=DEFAULT_ARK_MAX_TOKENS,
    )


def resolve_volcengine_ark_max_tokens_length_retry(analysis: AnalysisConfig) -> int:
    base = resolve_volcengine_ark_max_tokens(analysis)
    retry = _resolve_positive_int_env(
        (
            "YT2CLASS_ARK_MAX_TOKENS_LENGTH_RETRY",
            "ARK_MAX_TOKENS_LENGTH_RETRY",
            "VOLCENGINE_ARK_MAX_TOKENS_LENGTH_RETRY",
        ),
        configured=getattr(analysis, "ark_max_tokens_length_retry", None),
        default=DEFAULT_ARK_MAX_TOKENS_LENGTH_RETRY,
    )
    return max(retry, base)


def resolve_volcengine_ark_reasoning_effort(analysis: AnalysisConfig) -> str:
    for key in (
        "YT2CLASS_ARK_REASONING_EFFORT",
        "ARK_REASONING_EFFORT",
        "VOLCENGINE_ARK_REASONING_EFFORT",
    ):
        value = os.getenv(key)
        if value:
            return value.strip().lower()
    configured = getattr(analysis, "ark_reasoning_effort", None)
    if configured:
        return str(configured).strip().lower()
    return DEFAULT_ARK_REASONING_EFFORT


def resolve_volcengine_ark_timeout_seconds(analysis: AnalysisConfig) -> float:
    for key in (
        "YT2CLASS_ARK_TIMEOUT_SECONDS",
        "ARK_TIMEOUT_SECONDS",
        "VOLCENGINE_ARK_TIMEOUT_SECONDS",
    ):
        value = os.getenv(key)
        if value:
            try:
                parsed = float(value)
            except ValueError as error:
                raise ProviderError(f"invalid {key}={value!r}") from error
            if parsed <= 0:
                raise ProviderError(f"{key} must be positive")
            return parsed
    configured = getattr(analysis, "ark_timeout_seconds", None)
    if configured is not None and float(configured) > 0:
        return float(configured)
    from yt2class.adapters.providers.openrouter import resolve_openrouter_timeout_seconds

    return resolve_openrouter_timeout_seconds(analysis)


class VolcengineArkPlanProvider(Provider):
    """HTTP vision provider for Ark Agent Plan; stages attach JSON on ``last_payload``."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        capabilities: ProviderCapabilities | None = None,
        client: httpx.Client | None = None,
        json_mode: ArkJsonMode = "auto",
        max_images_per_batch: int = DEFAULT_ARK_MAX_IMAGES_PER_BATCH,
        outline_max_visual_overview: int = DEFAULT_ARK_OUTLINE_MAX_VISUAL_OVERVIEW,
        segment_max_ocr_regions: int = DEFAULT_ARK_SEGMENT_MAX_OCR_REGIONS,
        segment_max_evidence_ids: int = DEFAULT_ARK_SEGMENT_MAX_EVIDENCE_IDS,
        segment_ocr_text_chars: int = DEFAULT_ARK_SEGMENT_OCR_TEXT_CHARS,
        max_tokens: int = DEFAULT_ARK_MAX_TOKENS,
        max_tokens_length_retry: int = DEFAULT_ARK_MAX_TOKENS_LENGTH_RETRY,
        reasoning_effort: str = DEFAULT_ARK_REASONING_EFFORT,
    ) -> None:
        retry_cap = max(max_tokens_length_retry, max_tokens)
        super().__init__(
            capabilities
            or volcengine_ark_capabilities(
                max_images=max_images_per_batch,
                max_output_tokens=retry_cap,
            )
        )
        if not api_key.strip():
            raise ProviderError(
                "VOLCENGINE_ARK_API_KEY is required for analysis.provider ark-plan"
            )
        self._api_key = api_key
        self._model = model
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._client = client
        self._json_mode = json_mode
        self._max_images_per_batch = max(1, max_images_per_batch)
        self._outline_max_visual_overview = max(1, outline_max_visual_overview)
        self._segment_max_ocr_regions = max(1, segment_max_ocr_regions)
        self._segment_max_evidence_ids = max(1, segment_max_evidence_ids)
        self._segment_ocr_text_chars = max(32, segment_ocr_text_chars)
        self._max_tokens = max(1024, max_tokens)
        self._max_tokens_length_retry = max(self._max_tokens, max_tokens_length_retry)
        self._reasoning_effort = reasoning_effort.strip().lower() or DEFAULT_ARK_REASONING_EFFORT
        self.last_payload: dict[str, Any] | None = None
        self._run_root: Path | None = None
        self._visual: VisualCatalogue | None = None

    @classmethod
    def from_config(
        cls, analysis: AnalysisConfig, *, client: httpx.Client | None = None
    ) -> VolcengineArkPlanProvider:
        max_images = resolve_volcengine_ark_max_images_per_batch(analysis)
        max_tokens = resolve_volcengine_ark_max_tokens(analysis)
        max_tokens_retry = resolve_volcengine_ark_max_tokens_length_retry(analysis)
        return cls(
            api_key=resolve_volcengine_ark_api_key(),
            model=resolve_volcengine_ark_model(analysis),
            endpoint=resolve_volcengine_ark_endpoint(),
            client=client,
            json_mode=resolve_volcengine_ark_json_mode(analysis),
            timeout_seconds=resolve_volcengine_ark_timeout_seconds(analysis),
            capabilities=volcengine_ark_capabilities(
                max_images=max_images,
                max_output_tokens=max_tokens_retry,
            ),
            max_images_per_batch=max_images,
            max_tokens=max_tokens,
            max_tokens_length_retry=max_tokens_retry,
            reasoning_effort=resolve_volcengine_ark_reasoning_effort(analysis),
            outline_max_visual_overview=resolve_volcengine_ark_outline_max_visual_overview(
                analysis
            ),
            segment_max_ocr_regions=resolve_volcengine_ark_segment_max_ocr_regions(analysis),
            segment_max_evidence_ids=resolve_volcengine_ark_segment_max_evidence_ids(analysis),
            segment_ocr_text_chars=resolve_volcengine_ark_segment_ocr_text_chars(analysis),
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
        limit = min(limit, self._max_images_per_batch)
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

    @staticmethod
    def _cap_evidence_id_lists(
        payload: dict[str, Any],
        *,
        max_ids: int,
    ) -> tuple[dict[str, Any], bool]:
        keys = ("evidence_ids", "allowed_evidence_ids")
        lists = {
            key: payload.get(key)
            for key in keys
            if isinstance(payload.get(key), list)
        }
        if not lists:
            return payload, False
        longest = max(len(items) for items in lists.values())
        if longest <= max_ids:
            return payload, False

        merged: list[str] = []
        for items in lists.values():
            merged.extend(str(item) for item in items)
        unique = sorted(set(merged))
        ocr_ids = [item for item in unique if item.startswith("ocr-")]
        other_ids = [item for item in unique if not item.startswith("ocr-")]
        budget_for_ocr = max(0, max_ids - len(other_ids))
        kept = sorted(set(other_ids) | set(ocr_ids[:budget_for_ocr]))
        trimmed = dict(payload)
        for key in keys:
            if key in trimmed and isinstance(trimmed[key], list):
                trimmed[key] = kept
        trimmed["evidence_ids_omitted"] = max(0, len(unique) - len(kept))
        if len(ocr_ids) > budget_for_ocr:
            trimmed["ocr_evidence_ids_omitted"] = len(ocr_ids) - budget_for_ocr
        return trimmed, True

    def _trim_segment_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        trimmed, changed = self._cap_evidence_id_lists(
            payload,
            max_ids=self._segment_max_evidence_ids,
        )
        ocr = trimmed.get("ocr")
        if isinstance(ocr, list) and len(ocr) > self._segment_max_ocr_regions:
            trimmed = dict(trimmed)
            trimmed["ocr"] = ocr[: self._segment_max_ocr_regions]
            trimmed["ocr_regions_omitted"] = len(ocr) - self._segment_max_ocr_regions
            changed = True
        if isinstance(trimmed.get("ocr"), list):
            compact_ocr: list[dict[str, Any]] = []
            for row in trimmed["ocr"]:
                if not isinstance(row, dict):
                    continue
                text = row.get("text")
                if isinstance(text, str) and len(text) > self._segment_ocr_text_chars:
                    compact_ocr.append(
                        {
                            **row,
                            "text": text[: self._segment_ocr_text_chars] + "…",
                        }
                    )
                    changed = True
                else:
                    compact_ocr.append(row)
            if changed:
                trimmed = dict(trimmed)
                trimmed["ocr"] = compact_ocr
        return trimmed if changed else payload

    def _trim_outline_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        overview = payload.get("visual_overview")
        if not isinstance(overview, list):
            return payload
        cap = self._outline_max_visual_overview
        if len(overview) <= cap:
            return payload
        trimmed = dict(payload)
        trimmed["visual_overview"] = overview[:cap]
        trimmed["visual_overview_omitted"] = len(overview) - cap
        return trimmed

    def _payload_for_prompt(self, request: ModelRequest, payload: dict[str, Any]) -> dict[str, Any]:
        if request.role == "outline":
            return self._trim_outline_payload(payload)
        if request.role == "segment":
            return self._trim_segment_payload(payload)
        return payload

    def _build_messages(
        self,
        request: ModelRequest,
        payload: dict[str, Any],
        *,
        strengthen_json_reminder: bool = False,
    ) -> list[dict[str, Any]]:
        prompt = str(payload.get("prompt") or "Follow the JSON schema implied by your role.")
        payload_for_text = self._payload_for_prompt(request, payload)
        serialized = json.dumps(payload_for_text, ensure_ascii=False, sort_keys=True)
        reminder = ROLE_JSON_REMINDERS.get(request.role, "Return only the specified JSON object.")
        extra = f"\n\n{_LENGTH_RETRY_JSON_REMINDER}" if strengthen_json_reminder else ""
        text = (
            f"{prompt}\n\n"
            "Respond with a single JSON object only. No markdown fences or commentary.\n\n"
            f"Request role: {request.role}\n"
            f"Payload:\n{serialized}\n\n"
            f"{reminder}{extra}"
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
    def _message_strings(message: dict[str, Any]) -> tuple[str, str]:
        content = message.get("content", "")
        content_text = ""
        if isinstance(content, str):
            content_text = content
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
            content_text = "".join(parts)
        reasoning = message.get("reasoning_content")
        reasoning_text = reasoning if isinstance(reasoning, str) else ""
        return content_text, reasoning_text

    @staticmethod
    def _choice_fields(data: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError("Ark Plan response missing choices")
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError("Ark Plan response choice malformed")
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("Ark Plan response missing message")
        finish_reason = str(choice.get("finish_reason") or "")
        routed_model = str(data.get("model") or "")
        return message, finish_reason, routed_model

    @classmethod
    def _response_diagnostics(
        cls,
        data: dict[str, Any],
        *,
        request_model: str,
    ) -> dict[str, Any]:
        message, finish_reason, routed_model = cls._choice_fields(data)
        content_text, reasoning_text = cls._message_strings(message)
        return {
            "request_model": request_model,
            "routed_model": routed_model or request_model,
            "finish_reason": finish_reason,
            "content_len": len(content_text),
            "reasoning_len": len(reasoning_text),
            "content_has_brace": "{" in content_text,
            "reasoning_has_brace": "{" in reasoning_text,
        }

    @staticmethod
    def _text_for_json_parse(content_text: str, reasoning_text: str) -> str:
        content = content_text.strip()
        if content:
            return content_text
        reasoning = reasoning_text.strip()
        if reasoning and "{" in reasoning:
            return reasoning_text
        if reasoning:
            return reasoning_text
        raise ProviderError("Ark Plan response did not contain text or reasoning content")

    @classmethod
    def _should_retry_length_truncation(cls, data: dict[str, Any], content_text: str, reasoning_text: str) -> bool:
        _, finish_reason, _ = cls._choice_fields(data)
        if finish_reason != "length":
            return False
        if "{" in content_text:
            return False
        if "{" in reasoning_text:
            return False
        return True

    def _persist_failure_diagnostics(
        self,
        request: ModelRequest,
        diagnostics: dict[str, Any],
        *,
        error: str,
    ) -> None:
        if self._run_root is None:
            return
        path = self._run_root / "logs" / "ark-plan-failures.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            **diagnostics,
            "request_id": request.request_id,
            "role": request.role,
            "error": error[:500],
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

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
            return "Ark Plan request failed"
        parts: list[str] = []
        message = error.get("message")
        if message:
            parts.append(str(message))
        code = error.get("code")
        if code is not None:
            parts.append(f"code={code}")
        return "; ".join(parts) if parts else "Ark Plan request failed"

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.status_code < 400:
            return
        detail = "Ark Plan request failed"
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
        max_tokens: int | None = None,
        strengthen_json_reminder: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "temperature": 0.1,
            "max_tokens": max_tokens if max_tokens is not None else self._max_tokens,
            "messages": self._build_messages(
                request,
                payload,
                strengthen_json_reminder=strengthen_json_reminder,
            ),
            "reasoning": {"effort": self._reasoning_effort},
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

    @staticmethod
    def _count_images_in_body(body: dict[str, Any]) -> int:
        count = 0
        for message in body.get("messages") or []:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    count += 1
        return count

    def _log_request_start(self, body: dict[str, Any]) -> None:
        parsed = urlparse(self._endpoint)
        logger.info(
            "Ark Plan request start model=%s host=%s path=%s image_count=%d "
            "max_tokens=%s timeout_seconds=%.0f reasoning_effort=%s",
            self._model,
            parsed.netloc,
            parsed.path or "/",
            self._count_images_in_body(body),
            body.get("max_tokens"),
            self._timeout,
            (body.get("reasoning") or {}).get("effort"),
        )

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        self._log_request_start(body)
        client = self._client or httpx.Client(timeout=self._httpx_timeout())
        owns_client = self._client is None
        try:
            response = post_json_with_wall_clock_deadline(
                client,
                self._endpoint,
                headers=self._headers(),
                json_body=body,
                deadline_seconds=self._timeout,
                error_label="Ark Plan request",
            )
            self._raise_for_status(response)
            data = response.json()
        except RetryableError:
            raise
        except httpx.ReadTimeout as error:
            raise RetryableError(f"Ark Plan read timed out after {self._timeout:.0f}s") from error
        except httpx.ConnectTimeout as error:
            raise RetryableError(
                f"Ark Plan connect timed out after {min(30.0, self._timeout):.0f}s"
            ) from error
        except httpx.TimeoutException as error:
            raise RetryableError(f"Ark Plan request timed out: {error}") from error
        except httpx.HTTPError as error:
            raise RetryableError(f"Ark Plan transport error: {error.__class__.__name__}") from error
        finally:
            if owns_client:
                client.close()
        if not isinstance(data, dict):
            raise ProviderError("Ark Plan response was not a JSON object")
        return data

    def _complete_with_json_mode(
        self,
        request: ModelRequest,
        payload: dict[str, Any],
        *,
        use_structured_output: bool,
    ) -> ModelResult:
        attempts: list[tuple[int, bool]] = [
            (self._max_tokens, False),
            (self._max_tokens_length_retry, True),
        ]
        last_error = "Ark Plan returned invalid JSON"
        last_diag: dict[str, Any] | None = None
        data: dict[str, Any] | None = None
        for index, (token_budget, strengthen) in enumerate(attempts):
            body = self._build_request_body(
                request,
                payload,
                use_structured_output=use_structured_output,
                max_tokens=token_budget,
                strengthen_json_reminder=strengthen,
            )
            data = self._post(body)
            message, _, _ = self._choice_fields(data)
            content_text, reasoning_text = self._message_strings(message)
            diag = self._response_diagnostics(data, request_model=self._model)
            last_diag = diag
            if (
                index == 0
                and self._should_retry_length_truncation(data, content_text, reasoning_text)
            ):
                logger.info(
                    "Ark Plan length truncation without JSON; retrying request_id=%s "
                    "max_tokens=%d routed_model=%s",
                    request.request_id,
                    self._max_tokens_length_retry,
                    diag.get("routed_model"),
                )
                continue
            try:
                text = self._text_for_json_parse(content_text, reasoning_text)
            except ProviderError as error:
                last_error = str(error)
                if index == 0 and self._should_retry_length_truncation(
                    data, content_text, reasoning_text
                ):
                    continue
                if last_diag is not None:
                    self._persist_failure_diagnostics(request, last_diag, error=last_error)
                raise MissingStructuredOutput(
                    f"Ark Plan returned no parseable JSON for {request.request_id}: {error}"
                ) from error
            try:
                structured = parse_model_json(text)
            except Exception as error:
                message = str(error)
                last_error = message
                if index == 0 and (
                    self._should_retry_length_truncation(data, content_text, reasoning_text)
                    or "json object" in message.lower()
                ):
                    continue
                if "Unterminated" in message or "Expecting value" in message:
                    if last_diag is not None:
                        self._persist_failure_diagnostics(request, last_diag, error=message)
                    raise InvalidJsonResponse(
                        f"Ark Plan returned truncated JSON for {request.request_id}: {error}"
                    ) from error
                if last_diag is not None:
                    self._persist_failure_diagnostics(request, last_diag, error=message)
                raise MissingStructuredOutput(
                    f"Ark Plan returned invalid JSON for {request.request_id}: {error}"
                ) from error
            assert data is not None
            return ModelResult(
                request_id=request.request_id,
                structured=structured,
                usage=self._usage_from_response(request, data),
            )
        if last_diag is not None:
            self._persist_failure_diagnostics(request, last_diag, error=last_error)
        raise MissingStructuredOutput(
            f"Ark Plan returned invalid JSON for {request.request_id}: {last_error}"
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
            raise ProviderError("Ark Plan provider missing stage payload (last_payload)")
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
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "ArkJsonMode",
    "VolcengineArkPlanProvider",
    "resolve_volcengine_ark_api_key",
    "resolve_volcengine_ark_endpoint",
    "resolve_volcengine_ark_json_mode",
    "resolve_volcengine_ark_model",
    "resolve_volcengine_ark_max_tokens",
    "resolve_volcengine_ark_max_tokens_length_retry",
    "resolve_volcengine_ark_reasoning_effort",
    "resolve_volcengine_ark_max_images_per_batch",
    "resolve_volcengine_ark_outline_max_visual_overview",
    "resolve_volcengine_ark_segment_max_evidence_ids",
    "resolve_volcengine_ark_segment_max_ocr_regions",
    "resolve_volcengine_ark_segment_ocr_text_chars",
    "resolve_volcengine_ark_timeout_seconds",
    "volcengine_ark_capabilities",
]
