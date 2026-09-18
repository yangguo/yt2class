"""Hard wall-clock limits for blocking HTTP clients (httpx keepalive-safe)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Callable, TypeVar

import httpx

from yt2class.orchestration.retry import RetryableError

T = TypeVar("T")


def run_with_wall_clock_deadline(
    fn: Callable[[], T],
    *,
    deadline_seconds: float,
    on_deadline: Callable[[], None] | None = None,
    error_message: str,
) -> T:
    """Run ``fn`` in a worker thread and abort after ``deadline_seconds`` wall time."""

    if deadline_seconds <= 0:
        raise ValueError("deadline_seconds must be positive")
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(fn)
    try:
        return future.result(timeout=deadline_seconds)
    except FuturesTimeoutError:
        if on_deadline is not None:
            on_deadline()
        raise RetryableError(error_message)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def post_json_with_wall_clock_deadline(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    json_body: dict[str, Any],
    deadline_seconds: float,
    error_label: str,
) -> httpx.Response:
    """POST JSON with httpx read timeout plus an independent wall-clock cap."""

    def _post() -> httpx.Response:
        return client.post(url, headers=headers, json=json_body)

    def _abort() -> None:
        client.close()

    return run_with_wall_clock_deadline(
        _post,
        deadline_seconds=deadline_seconds,
        on_deadline=_abort,
        error_message=(
            f"{error_label} exceeded wall-clock deadline of {deadline_seconds:.0f}s"
        ),
    )


__all__ = ["post_json_with_wall_clock_deadline", "run_with_wall_clock_deadline"]
