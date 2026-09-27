"""Provider-neutral image adapter for OpenAI-compatible chat/completions APIs."""

from __future__ import annotations

import os
from typing import cast
from urllib.parse import urlsplit

import httpx

from yt2class.adapters.providers.base import ProviderError
from yt2class.adapters.providers.openrouter import (
    DEFAULT_TIMEOUT_SECONDS,
    OpenRouterJsonMode,
    OpenRouterProvider,
)
from yt2class.config import AnalysisConfig


class OpenAICompatibleImageProvider(OpenRouterProvider):
    """Use the existing frame payload format with a configurable compatible API."""

    PROVIDER_LABEL = "OpenAI-compatible image"

    @classmethod
    def from_config(
        cls, analysis: AnalysisConfig, *, client: httpx.Client | None = None
    ) -> OpenAICompatibleImageProvider:
        endpoint = os.getenv("YT2CLASS_IMAGE_ENDPOINT", "").strip()
        api_key = os.getenv("YT2CLASS_IMAGE_API_KEY", "").strip()
        model = (analysis.model or os.getenv("YT2CLASS_IMAGE_MODEL", "")).strip()
        if not endpoint:
            raise ProviderError("YT2CLASS_IMAGE_ENDPOINT is required for openai-compatible image analysis")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or not parsed.path.rstrip("/").endswith("/chat/completions"):
            raise ProviderError("YT2CLASS_IMAGE_ENDPOINT must be an HTTP(S) chat/completions URL")
        if not api_key:
            raise ProviderError("YT2CLASS_IMAGE_API_KEY is required for openai-compatible image analysis")
        if not model:
            raise ProviderError("YT2CLASS_IMAGE_MODEL or analysis.model is required for openai-compatible image analysis")
        raw_json_mode = os.getenv("YT2CLASS_IMAGE_JSON_MODE", "auto").strip().lower()
        if raw_json_mode not in {"auto", "on", "off"}:
            raise ProviderError("YT2CLASS_IMAGE_JSON_MODE must be auto, on, or off")
        raw_timeout = os.getenv("YT2CLASS_IMAGE_TIMEOUT_SECONDS", "").strip()
        try:
            timeout = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_SECONDS
        except ValueError as error:
            raise ProviderError("YT2CLASS_IMAGE_TIMEOUT_SECONDS must be a positive number") from error
        if timeout <= 0:
            raise ProviderError("YT2CLASS_IMAGE_TIMEOUT_SECONDS must be a positive number")
        return cls(
            api_key=api_key,
            model=model,
            endpoint=endpoint,
            timeout_seconds=timeout,
            json_mode=cast(OpenRouterJsonMode, raw_json_mode),
            client=client,
        )

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }


__all__ = ["OpenAICompatibleImageProvider"]
