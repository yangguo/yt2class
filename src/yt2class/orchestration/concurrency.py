"""Bounded parallelism for independent provider-bound stage work."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from typing import Callable, TypeVar

from yt2class.adapters.providers.base import RequestCancelled

T = TypeVar("T")
R = TypeVar("R")

MAX_PROVIDER_CONCURRENCY = 2


def map_parallel(
    items: list[T],
    fn: Callable[[T], R],
    *,
    max_workers: int = MAX_PROVIDER_CONCURRENCY,
    cancel_event: Event | None = None,
) -> list[R]:
    """Run ``fn`` over ``items`` with a bounded pool; output order matches input."""

    if cancel_event is not None and cancel_event.is_set():
        raise RequestCancelled("parallel work cancelled before start")
    if not items:
        return []
    if len(items) == 1:
        return [fn(items[0])]
    workers = min(max(max_workers, 1), len(items), MAX_PROVIDER_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


__all__ = ["MAX_PROVIDER_CONCURRENCY", "map_parallel"]
