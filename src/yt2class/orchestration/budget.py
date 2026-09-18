"""Run-level model and media budgets (tokens, images, video, estimated USD)."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


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


@dataclass(frozen=True)
class BudgetReservation:
    model_calls: int
    input_tokens: int
    output_tokens: int
    image_count: int
    video_seconds: float
    estimated_usd: float


@dataclass
class RunBudget:
    limits: BudgetLimits
    consumed: BudgetSnapshot = field(default_factory=BudgetSnapshot)
    paused: bool = False
    _reserved: BudgetSnapshot = field(default_factory=BudgetSnapshot, repr=False)
    _lock: Lock = field(default_factory=Lock, repr=False)

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
        with self._lock:
            return self._would_exceed_unlocked(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                image_count=image_count,
                video_seconds=video_seconds,
                estimated_usd=estimated_usd,
                model_calls=model_calls,
            )

    def _would_exceed_unlocked(
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
        next_calls = self.consumed.model_calls + self._reserved.model_calls + model_calls
        if next_calls > self.limits.max_model_calls:
            return "max_model_calls"
        if (
            self.consumed.input_tokens + self._reserved.input_tokens + input_tokens
            > self.limits.max_input_tokens
        ):
            return "max_input_tokens"
        if (
            self.consumed.output_tokens + self._reserved.output_tokens + output_tokens
            > self.limits.max_output_tokens
        ):
            return "max_output_tokens"
        if (
            self.consumed.image_count + self._reserved.image_count + image_count
            > self.limits.max_images
        ):
            return "max_images"
        if (
            self.consumed.video_seconds + self._reserved.video_seconds + video_seconds
            > self.limits.max_video_seconds
        ):
            return "max_video_seconds"
        if (
            self.consumed.estimated_usd + self._reserved.estimated_usd + estimated_usd
            > self.limits.max_estimated_usd
        ):
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
        with self._lock:
            reason = self._would_exceed_unlocked(
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

    def reserve(
        self,
        *,
        input_tokens: int = 0,
        output_tokens: int = 0,
        image_count: int = 0,
        video_seconds: float = 0.0,
        estimated_usd: float = 0.0,
        model_calls: int = 1,
    ) -> BudgetReservation:
        reservation = BudgetReservation(
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            image_count=image_count,
            video_seconds=video_seconds,
            estimated_usd=estimated_usd,
        )
        with self._lock:
            reason = self._would_exceed_unlocked(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                image_count=image_count,
                video_seconds=video_seconds,
                estimated_usd=estimated_usd,
                model_calls=model_calls,
            )
            if reason is not None:
                raise BudgetExceeded(f"budget exceeded: {reason}", kind=reason)
            self._apply_unlocked(self._reserved, reservation, direction=1)
        return reservation

    def release(self, reservation: BudgetReservation) -> None:
        with self._lock:
            self._apply_unlocked(self._reserved, reservation, direction=-1)

    def settle(
        self,
        reservation: BudgetReservation,
        *,
        input_tokens: int,
        output_tokens: int,
        image_count: int,
        video_seconds: float,
        estimated_usd: float = 0.0,
        model_calls: int = 1,
    ) -> str | None:
        actual = BudgetReservation(
            model_calls=model_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            image_count=image_count,
            video_seconds=video_seconds,
            estimated_usd=estimated_usd,
        )
        with self._lock:
            self._apply_unlocked(self._reserved, reservation, direction=-1)
            self._apply_unlocked(self.consumed, actual, direction=1)
            reason = self._limit_exceeded_unlocked()
            if reason is not None:
                self.paused = True
            return reason

    @staticmethod
    def _apply_unlocked(
        target: BudgetSnapshot,
        amount: BudgetReservation,
        *,
        direction: int,
    ) -> None:
        target.model_calls += direction * amount.model_calls
        target.input_tokens += direction * amount.input_tokens
        target.output_tokens += direction * amount.output_tokens
        target.image_count += direction * amount.image_count
        target.video_seconds += direction * amount.video_seconds
        target.estimated_usd += direction * amount.estimated_usd

    def _limit_exceeded_unlocked(self) -> str | None:
        if self.consumed.model_calls > self.limits.max_model_calls:
            return "max_model_calls"
        if self.consumed.input_tokens > self.limits.max_input_tokens:
            return "max_input_tokens"
        if self.consumed.output_tokens > self.limits.max_output_tokens:
            return "max_output_tokens"
        if self.consumed.image_count > self.limits.max_images:
            return "max_images"
        if self.consumed.video_seconds > self.limits.max_video_seconds:
            return "max_video_seconds"
        if self.consumed.estimated_usd > self.limits.max_estimated_usd:
            return "max_estimated_usd"
        return None

    def pause(self, reason: str) -> None:
        with self._lock:
            self.paused = True
        _ = reason


__all__ = [
    "BudgetExceeded",
    "BudgetLimits",
    "BudgetReservation",
    "BudgetSnapshot",
    "RunBudget",
]
