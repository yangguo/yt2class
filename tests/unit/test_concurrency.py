from __future__ import annotations

import threading

from yt2class.orchestration.concurrency import map_parallel


def test_map_parallel_preserves_order():
    seen: list[int] = []

    def work(value: int) -> int:
        seen.append(value)
        return value * 2

    result = map_parallel([1, 2, 3, 4], work)
    assert result == [2, 4, 6, 8]
    assert sorted(seen) == [1, 2, 3, 4]


def test_map_parallel_respects_worker_cap():
    active = 0
    peak = 0
    lock = threading.Lock()

    def work(value: int) -> int:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.01)
        with lock:
            active -= 1
        return value

    map_parallel(list(range(6)), work, max_workers=2)
    assert peak <= 2
