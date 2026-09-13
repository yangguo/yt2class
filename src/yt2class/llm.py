"""Optional vision-model adapter and deterministic offline fallback."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

import httpx

from yt2class.lesson_plan import LessonPlan, PlanValidationError, plan_to_dict, validate_plan
from yt2class.scenes import FrameCandidate
from yt2class.subtitles import Caption, nearby_text


class ModelError(RuntimeError):
    """Raised when a configured model cannot return a valid lesson plan."""


@dataclass(frozen=True)
class ModelConfig:
    endpoint: str
    api_key: str
    model: str
    provider: str = "openai"
    timeout_seconds: float = 180.0

    @classmethod
    def from_environment(cls) -> "ModelConfig | None":
        explicit_url = os.getenv("YT2CLASS_MODEL_URL")
        explicit_key = os.getenv("YT2CLASS_MODEL_KEY")
        if explicit_url and explicit_key:
            provider = "anthropic" if explicit_url.rstrip("/").endswith("/messages") else "openai"
            return cls(
                endpoint=explicit_url,
                api_key=explicit_key,
                model=os.getenv("YT2CLASS_MODEL", "gpt-4.1-mini"),
                provider=provider,
            )

        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            return cls(
                endpoint=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
                + "/chat/completions",
                api_key=openai_key,
                model=os.getenv("YT2CLASS_MODEL", "gpt-4.1-mini"),
            )

        anthropic_key = os.getenv("ANTHROPIC_AUTH_TOKEN")
        if anthropic_key:
            base = os.getenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com").rstrip("/")
            if "deepseek.com" in base:
                parsed = urlsplit(base if "://" in base else f"https://{base}")
                endpoint = f"{parsed.scheme}://{parsed.netloc}/v1/chat/completions"
                model = re.sub(r"\[.*\]$", "", os.getenv("ANTHROPIC_MODEL", "deepseek-v4-flash"))
                return cls(
                    endpoint=endpoint,
                    api_key=anthropic_key,
                    model=model,
                    provider="openai",
                )
            endpoint = base if base.endswith("/messages") else base + "/v1/messages"
            return cls(
                endpoint=endpoint,
                api_key=anthropic_key,
                model=os.getenv("ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
                provider="anthropic",
            )
        return None


@dataclass(frozen=True)
class SelectionDecision:
    plan: LessonPlan
    mode: str
    note: str | None = None


def parse_model_json(payload: str | Mapping[str, object]) -> dict[str, object]:
    """Parse a JSON object from plain or Markdown-fenced model output."""

    if isinstance(payload, Mapping):
        return dict(payload)
    text = payload.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ModelError("model response did not contain a JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ModelError(f"model response was not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ModelError("model response must be a JSON object")
    return value


def _short_text(text: str, limit: int = 100) -> str:
    return " ".join(text.split())[:limit]


def _choose_candidates(candidates: Sequence[FrameCandidate], max_slides: int) -> list[FrameCandidate]:
    if len(candidates) <= max_slides:
        return list(candidates)
    if max_slides <= 0:
        return []
    positions = [round(index * (len(candidates) - 1) / (max_slides - 1)) for index in range(max_slides)]
    return [candidates[position] for position in dict.fromkeys(positions)]


def offline_plan(
    course_title: str,
    candidates: Sequence[FrameCandidate],
    cues: Sequence[Caption],
    *,
    max_slides: int = 12,
) -> LessonPlan:
    """Create an honest, evidence-bound plan when no model credentials exist."""

    chosen = _choose_candidates(candidates, max_slides)
    if not chosen:
        raise ModelError("no candidate frames available for offline plan")
    slides = []
    for index, candidate in enumerate(chosen, start=1):
        context = _short_text(nearby_text(list(cues), candidate.timestamp))
        if context:
            title = f"字幕线索：{context[:54]}"
            explanation = (
                f"保留视频原始画面（{candidate.timestamp:.1f}s）。附近字幕为「{context}」；"
                "请结合板书和例句复习。"
            )
        else:
            title = f"课程画面 {index}"
            explanation = (
                f"保留视频原始画面（{candidate.timestamp:.1f}s），"
                "当前没有可用字幕，请回看该时间点核对板书。"
            )
        slides.append(
            {
                "frame_id": candidate.frame_id,
                "kind": "grammar" if index == 1 else "example",
                "title": title,
                "explanation_zh": explanation,
                "takeaway": "原始画面证据，按课程顺序复习。",
            }
        )
    summary = [
        f"本课主题：{course_title}",
        f"按时间顺序保留 {len(slides)} 张课程关键画面",
        "字幕不足时以视频原始板书为准，并可点击时间点回看",
    ]
    quiz = [
        {
            "prompt": f"请用自己的话说明「{course_title}」本课最重要的一点。",
            "answer": "回看对应原始画面和字幕后填写。",
        }
    ]
    return validate_plan(
        {
            "title": course_title,
            "subtitle": "原始视频画面讲义",
            "slides": slides,
            "summary": summary,
            "quiz": quiz,
        },
        {candidate.frame_id for candidate in candidates},
    )


def _data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _prompt(
    course_title: str,
    candidates: Sequence[FrameCandidate],
    cues: Sequence[Caption],
    max_slides: int,
) -> str:
    rows = []
    for candidate in candidates:
        context = _short_text(nearby_text(list(cues), candidate.timestamp), 160)
        rows.append(
            f"- frame_id={candidate.frame_id}, timestamp={candidate.timestamp:.3f}s, subtitle={context or '(none)'}"
        )
    return "\n".join(
        [
            "你是日语课程讲义编辑。只根据提供的原始课程截图和字幕上下文工作。",
            f"课程标题：{course_title}",
            f"最多选择 {max_slides} 张；按视频时间顺序，只保留新语法点、重要例句、表格或板书。",
            "不要生成、改写或裁剪图片；只返回 JSON，图片必须通过 frame_id 引用。",
            "每个 slide 必须包含 frame_id、kind(grammar/example/table/confusion/other)、title、explanation_zh，可选 takeaway。",
            "summary 是 1-8 条中文字符串；quiz 是包含 prompt 和 answer 的对象数组。",
            "候选帧：",
            *rows,
        ]
    )


def _extract_response_text(data: Mapping[str, object], provider: str) -> str:
    if provider == "anthropic":
        content = data.get("content")
        if isinstance(content, list):
            return "\n".join(
                block.get("text", "") for block in content if isinstance(block, Mapping)
            )
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message", {})
        if isinstance(message, Mapping):
            content = message.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return "\n".join(
                    block.get("text", "") for block in content if isinstance(block, Mapping)
                )
    raise ModelError("model response did not contain message text")


def _call_model(
    config: ModelConfig,
    prompt: str,
    candidates: Sequence[FrameCandidate],
) -> str:
    if config.provider == "anthropic":
        content: list[dict[str, object]] = [{"type": "text", "text": prompt}]
        for candidate in candidates:
            data_url = _data_url(candidate.path)
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": data_url.split(",", 1)[1],
                    },
                }
            )
        headers = {"x-api-key": config.api_key, "anthropic-version": "2023-06-01"}
        body = {
            "model": config.model,
            "max_tokens": 3500,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": content}],
        }
    else:
        content = [{"type": "text", "text": prompt}]
        content.extend(
            {
                "type": "image_url",
                "image_url": {"url": _data_url(candidate.path)},
            }
            for candidate in candidates
        )
        headers = {"Authorization": f"Bearer {config.api_key}"}
        body = {
            "model": config.model,
            "temperature": 0.1,
            "messages": [{"role": "user", "content": content}],
        }
    try:
        response = httpx.post(
            config.endpoint,
            headers=headers,
            json=body,
            timeout=config.timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise ModelError(f"model request failed: {error}") from error
    if not isinstance(data, Mapping):
        raise ModelError("model response was not a JSON object")
    return _extract_response_text(data, config.provider)


def select_plan(
    course_title: str,
    candidates: Sequence[FrameCandidate],
    cues: Sequence[Caption],
    *,
    selection_file: Path | None = None,
    max_slides: int = 12,
    config: ModelConfig | None = None,
) -> LessonPlan:
    """Load a reviewed plan, call a configured vision model, or use offline mode."""

    known = {candidate.frame_id for candidate in candidates}
    if selection_file is not None:
        try:
            payload = json.loads(selection_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ModelError(f"cannot read selection file: {error}") from error
        try:
            return validate_plan(payload, known)
        except PlanValidationError as error:
            raise ModelError(f"selection file is invalid: {error}") from error

    chosen = _choose_candidates(candidates, max_slides)
    config = config or ModelConfig.from_environment()
    if config is None:
        return offline_plan(course_title, chosen, cues, max_slides=max_slides)
    response_text = _call_model(config, _prompt(course_title, chosen, cues, max_slides), chosen)
    try:
        return validate_plan(parse_model_json(response_text), known)
    except PlanValidationError as error:
        raise ModelError(f"model lesson plan is invalid: {error}") from error


def select_plan_with_mode(
    course_title: str,
    candidates: Sequence[FrameCandidate],
    cues: Sequence[Caption],
    *,
    selection_file: Path | None = None,
    max_slides: int = 12,
    config: ModelConfig | None = None,
) -> SelectionDecision:
    """Select a plan and make model use or an offline fallback explicit."""

    if selection_file is not None:
        return SelectionDecision(
            plan=select_plan(
                course_title,
                candidates,
                cues,
                selection_file=selection_file,
                max_slides=max_slides,
                config=config,
            ),
            mode="reviewed",
        )
    resolved_config = config or ModelConfig.from_environment()
    if resolved_config is None:
        return SelectionDecision(
            plan=offline_plan(course_title, candidates, cues, max_slides=max_slides),
            mode="offline",
        )
    if "api.deepseek.com" in resolved_config.endpoint:
        return SelectionDecision(
            plan=offline_plan(course_title, candidates, cues, max_slides=max_slides),
            mode="offline-fallback",
            note="configured DeepSeek API is text-only; image frames were not sent",
        )
    try:
        return SelectionDecision(
            plan=select_plan(
                course_title,
                candidates,
                cues,
                max_slides=max_slides,
                config=resolved_config,
            ),
            mode="model",
        )
    except ModelError as error:
        return SelectionDecision(
            plan=offline_plan(course_title, candidates, cues, max_slides=max_slides),
            mode="offline-fallback",
            note=str(error),
        )
