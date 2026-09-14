"""Run-level model and media budgets (tokens, images, video, estimated USD)."""

from __future__ import annotations

from dataclasses import dataclass, field


class BudgetExceeded(RuntimeError):
    """Raised when a new provider request would exceed configured limits."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class BudgetLimits:
    max_model_calls: int = 100
    max_input_tokens: int = 2_000_000
    max_output_tokens: int = 500_000
    max_images: int = 500
    max_video_seconds: float = 3600.0
    max_estimated_usd: float = 5.0


@dataclass
class BudgetSnapshot:
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    image_count: int = 0
    video_seconds: float = 0.0
    estimated_usd: float = 0.0

    def as_usage(self) -> dict[str, float | int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "image_count": self.image_count,
            "video_seconds": self.video_seconds,
            "estimated_usd": self.estimated_usd,
        }


@dataclass
class RunBudget:
    limits: BudgetLimits
    consumed: BudgetSnapshot = field(default_factory=BudgetSnapshot)
    paused: bool = False

    def would_exceed(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        image_count: int = 0,
        video_seconds: float = 0.0,
        estimated_usd: float = 0.0,
        model_calls: int = 1,
    ) -> str | None:
        if self.paused:
            return "budget_paused"
        next_calls = self.consumed.model_calls + model_calls
        if next_calls > self.limits.max_model_calls:
            return "max_model_calls"
        if self.consumed.input_tokens + input_tokens > self.limits.max_input_tokens:
            return "max_input_tokens"
        if self.consumed.output_tokens + output_tokens > self.limits.max_output_tokens:
            return "max_output_tokens"
        if self.consumed.image_count + image_count > self.limits.max_images:
            return "max_images"
        if self.consumed.video_seconds + video_seconds > self.limits.max_video_seconds:
            return "max_video_seconds"
        if self.consumed.estimated_usd + estimated_usd > self.limits.max_estimated_usd:
            return "max_estimated_usd"
        return None

    def charge(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        image_count: int = 0,
        video_seconds: float = 0.0,
        estimated_usd: float = 0.0,
        model_calls: int = 1,
    ) -> None:
        reason = self.would_exceed(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            image_count=image_count,
            video_seconds=video_seconds,
            estimated_usd=estimated_usd,
            model_calls=model_calls,
        )
        if reason is not None:
            self.paused = True
            raise BudgetExceeded(f"budget exceeded: {reason}", kind=reason)
        self.consumed.model_calls += model_calls
        self.consumed.input_tokens += input_tokens
        self.consumed.output_tokens += output_tokens
        self.consumed.image_count += image_count
        self.consumed.video_seconds += video_seconds
        self.consumed.estimated_usd += estimated_usd

    def pause(self, reason: str) -> None:
        self.paused = True
        _ = reason


__all__ = ["BudgetExceeded", "BudgetLimits", "BudgetSnapshot", "RunBudget"]
