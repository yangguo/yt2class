"""Opt-in direct YouTube video preview through the Gemini Developer API."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import httpx

from yt2class.domain.source import SourceInput, SourceInputError

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent"
)
DEFAULT_PROMPT = """请根据这一个 YouTube 视频，生成供课程内容对照的中文分析。仅写视频中能确认的内容，不要补充外部知识；无法确认时明确说明。保留日文术语、板书原文、数字、否定词和条件。每个独立例句单独列出，尽量给出可回看的 [MM:SS] 时间戳；不要猜测时间戳。

输出以下部分：
- 核心摘要
- 主要观点或用法（包括接续条件）
- 重要事实、数据及全部可辨认的例句（附中文释义）
- 带时间戳的章节
- 值得注意的结论与仍不确定的内容
"""


class GeminiPreviewError(ValueError):
    """A direct video preview could not be produced."""


def collect_video_urls(urls: list[str], links: Path | None) -> list[str]:
    """Normalize and deduplicate one-video YouTube URLs from CLI inputs."""

    candidates = read_video_candidates(urls, links)
    normalized: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            source = SourceInput.from_value(candidate)
        except SourceInputError as error:
            raise ValueError(str(error)) from error
        if source.kind != "youtube":
            raise ValueError(f"Only YouTube video URLs are supported: {candidate}")
        if source.source_key not in seen:
            normalized.append(source.normalized_value)
            seen.add(source.source_key)
    return normalized


def read_video_candidates(urls: list[str], links: Path | None) -> list[str]:
    """Read a URL list without blocking valid items on another item's error."""

    candidates = list(urls)
    if links is not None:
        candidates.extend(
            line.strip()
            for line in links.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if not candidates:
        raise ValueError("Provide --url or --links with at least one YouTube video URL")
    return candidates


def generate_video_preview(
    url: str,
    *,
    api_key: str,
    client: httpx.Client,
    prompt: str = DEFAULT_PROMPT,
) -> dict[str, Any]:
    """Ask Gemini to analyze one public YouTube video without downloading it."""

    source = SourceInput.from_value(url)
    if source.kind != "youtube":
        raise GeminiPreviewError("Only YouTube video URLs are supported")
    if not api_key.strip():
        raise GeminiPreviewError("GEMINI_API_KEY is required")
    if not prompt.strip():
        raise GeminiPreviewError("The analysis prompt must not be empty")
    body = {
        "contents": [
            {
                "parts": [
                    {"file_data": {"file_uri": source.normalized_value}},
                    {"text": prompt},
                ]
            }
        ]
    }
    try:
        response = client.post(
            GEMINI_ENDPOINT,
            headers={"x-goog-api-key": api_key},
            json=body,
            timeout=180.0,
        )
    except httpx.RequestError as error:
        raise GeminiPreviewError(f"Gemini request failed: {type(error).__name__}") from error
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message", "request rejected")
        except (ValueError, AttributeError):
            message = "request rejected"
        safe_message = str(message).replace(api_key, "[REDACTED]")
        raise GeminiPreviewError(f"Gemini HTTP {response.status_code}: {safe_message}")
    try:
        data = response.json()
        candidate = data["candidates"][0]
        finish_reason = candidate.get("finishReason")
        text = "\n".join(
            part["text"] for part in candidate["content"]["parts"] if part.get("text")
        ).strip()
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise GeminiPreviewError("Gemini response has no usable text") from error
    if finish_reason not in (None, "STOP"):
        raise GeminiPreviewError(f"Gemini response incomplete: {finish_reason}")
    if not text:
        raise GeminiPreviewError("Gemini response has no usable text")
    return {
        "url": source.normalized_value,
        "model": GEMINI_MODEL,
        "prompt": prompt,
        "text": text,
        "usage": data.get("usageMetadata", {}),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_video_preview(preview: dict[str, Any], output: Path) -> tuple[Path, Path]:
    """Save model text and its provenance without storing credentials."""

    source = SourceInput.from_value(preview["url"])
    if source.kind != "youtube" or source.video_id is None:
        raise ValueError("Preview source must be a YouTube video")
    output.mkdir(parents=True, exist_ok=True)
    markdown = output / f"{source.video_id}.md"
    record = output / f"{source.video_id}.json"
    markdown.write_text(str(preview["text"]).rstrip() + "\n", encoding="utf-8")
    record.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown, record
