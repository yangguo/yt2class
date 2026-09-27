"""Opt-in direct YouTube video analysis through the Gemini Developer API."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from yt2class.domain.source import SourceInput, SourceInputError

GEMINI_MODEL = "gemini-3.8-flash"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
DEFAULT_PROMPT = """请根据这一个 YouTube 视频，生成供课程内容对照的中文分析。仅写视频中能确认的内容，不要补充外部知识；无法确认时明确说明。保留日文术语、板书原文、数字、否定词和条件。每个独立例句单独列出，尽量给出可回看的 [MM:SS] 时间戳；不要猜测时间戳。

输出以下部分：
- 核心摘要
- 主要观点或用法（包括接续条件）
- 重要事实、数据及全部可辨认的例句（附中文释义）
- 带时间戳的章节
- 值得注意的结论与仍不确定的内容
"""


class GeminiPreviewError(ValueError):
    """A direct video analysis could not be produced."""


@dataclass(frozen=True)
class VideoEntry:
    url: str
    title: str | None = None
    playlist_url: str | None = None
    playlist_index: int | None = None


def read_video_candidates(urls: list[str], links: Path | None) -> list[str]:
    candidates = list(urls)
    if links is not None:
        candidates.extend(
            line.strip()
            for line in links.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if not candidates:
        raise ValueError("Provide --url or --links with at least one YouTube video or playlist URL")
    return candidates


def _is_playlist_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    query = parse_qs(parsed.query)
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"
        }
        and "list" in query
        and parsed.path.rstrip("/").lower() == "/playlist"
    )


def _playlist_entries(url: str) -> list[VideoEntry]:
    command = [
        "yt-dlp", "--ignore-config", "--flat-playlist", "--dump-single-json",
        "--skip-download", "--no-warnings", "--", url,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    except FileNotFoundError as error:
        raise ValueError("yt-dlp is required to expand YouTube playlists") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("yt-dlp timed out while expanding the playlist") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "playlist lookup failed").strip()
        raise ValueError(f"yt-dlp could not read playlist: {detail}") from error
    try:
        data = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("yt-dlp returned invalid playlist metadata") from error
    if not isinstance(data, dict):
        raise ValueError("yt-dlp returned invalid playlist metadata")
    entries: list[VideoEntry] = []
    for index, item in enumerate(data.get("entries") or [], start=1):
        if not item or not item.get("id"):
            continue
        try:
            source = SourceInput.from_value(f"https://www.youtube.com/watch?v={item['id']}")
        except SourceInputError:
            continue
        entries.append(VideoEntry(source.normalized_value, item.get("title"), url, index))
    if not entries:
        raise ValueError("No accessible video entries found in playlist")
    return entries


def expand_video_inputs(candidates: list[str]) -> list[VideoEntry]:
    """Expand playlist URLs with yt-dlp metadata and deduplicate in input order."""
    entries: list[VideoEntry] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            expanded = _playlist_entries(candidate) if _is_playlist_url(candidate) else [
                VideoEntry(SourceInput.from_value(candidate).normalized_value)
            ]
        except SourceInputError as error:
            raise ValueError(str(error)) from error
        for entry in expanded:
            source = SourceInput.from_value(entry.url)
            if source.source_key not in seen:
                seen.add(source.source_key)
                entries.append(entry)
    if not entries:
        raise ValueError("No YouTube video URLs were found")
    return entries


def collect_video_urls(urls: list[str], links: Path | None) -> list[str]:
    """Compatibility helper returning normalized URLs for direct video inputs."""
    return [entry.url for entry in expand_video_inputs(read_video_candidates(urls, links))]


def generate_video_preview(
    url: str,
    *,
    api_key: str,
    client: httpx.Client,
    prompt: str = DEFAULT_PROMPT,
    model: str = GEMINI_MODEL,
) -> dict[str, Any]:
    source = SourceInput.from_value(url)
    if not api_key.strip():
        raise GeminiPreviewError("GEMINI_API_KEY is required")
    if not prompt.strip():
        raise GeminiPreviewError("The analysis prompt must not be empty")
    model = model.strip()
    if not _MODEL_NAME.fullmatch(model):
        raise GeminiPreviewError("Invalid Gemini model name")
    body = {"contents": [{"parts": [
        {"file_data": {"file_uri": source.normalized_value}}, {"text": prompt}
    ]}]}
    try:
        response = client.post(
            GEMINI_ENDPOINT.format(model=model), headers={"x-goog-api-key": api_key},
            json=body, timeout=180.0,
        )
    except httpx.RequestError as error:
        raise GeminiPreviewError(f"Gemini request failed: {type(error).__name__}") from error
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message", "request rejected")
        except (ValueError, AttributeError):
            message = "request rejected"
        raise GeminiPreviewError(
            f"Gemini HTTP {response.status_code}: {str(message).replace(api_key, '[REDACTED]')}"
        )
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
        "url": source.normalized_value, "model": model, "prompt": prompt, "text": text,
        "usage": data.get("usageMetadata", {}),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def write_video_preview(preview: dict[str, Any], output: Path) -> tuple[Path, Path]:
    source = SourceInput.from_value(preview["url"])
    if source.video_id is None:
        raise ValueError("Preview source must be a YouTube video")
    output.mkdir(parents=True, exist_ok=True)
    markdown = output / f"{source.video_id}.md"
    record = output / f"{source.video_id}.json"
    markdown.write_text(str(preview["text"]).rstrip() + "\n", encoding="utf-8")
    record.write_text(json.dumps(preview, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown, record


def write_batch_manifest(records: list[dict[str, Any]], output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    path = output / "batch-manifest.json"
    payload = {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "items": records}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
