"""HTTP/provider retry policy: Retry-After, jitter, non-retryable status codes."""

from __future__ import annotations

from dataclasses import dataclass
import random
import time
from typing import Callable, TypeVar

T = TypeVar("T")


class RetryableError(RuntimeError):
    """Transient failure that may succeed on retry."""

    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class NonRetryableError(RuntimeError):
    """Failure that must not be retried blindly (auth, modality, etc.)."""


class InvalidJsonResponse(RetryableError):
    """Structured output parse failure; at most one repair attempt upstream."""


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.2

    def delay_before_attempt(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None and retry_after > 0:
            # Honor server Retry-After; only exponential backoff is capped.
            base = retry_after
        else:
            base = min(self.base_delay_seconds * (2 ** max(0, attempt - 1)), self.max_delay_seconds)
        jitter = base * self.jitter_ratio * random.random()
        return base + jitter


def classify_http_status(status_code: int) -> type[Exception]:
    if status_code in {401, 403}:
        return NonRetryableError
    if status_code == 429 or status_code >= 500:
        return RetryableError
    if status_code >= 400:
        return NonRetryableError
    return RetryableError


def parse_retry_after(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return float(header_value.strip())
    except ValueError:
        return None


def call_with_retry(
    operation: Callable[[], T],
    *,
    policy: RetryPolicy | None = None,
    sleep: Callable[[float], None] = time.sleep,
    is_retryable: Callable[[BaseException], bool] | None = None,
) -> T:
    cfg = policy or RetryPolicy()
    last_error: BaseException | None = None
    for attempt in range(1, cfg.max_attempts + 1):
        try:
            return operation()
        except Exception as error:  # noqa: BLE001 - policy classifies retryability
            last_error = error
            retryable = is_retryable(error) if is_retryable is not None else isinstance(error, RetryableError)
            if not retryable or attempt >= cfg.max_attempts:
                raise
            retry_after = getattr(error, "retry_after", None)
            sleep(cfg.delay_before_attempt(attempt, retry_after))
    assert last_error is not None
    raise last_error


__all__ = [
    "InvalidJsonResponse",
    "NonRetryableError",
    "RetryPolicy",
    "RetryableError",
    "call_with_retry",
    "classify_http_status",
    "parse_retry_after",
]
