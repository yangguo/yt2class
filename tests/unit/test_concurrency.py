from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from yt2class.orchestration.concurrency import MAX_PROVIDER_CONCURRENCY, map_parallel
from yt2class.stages.edit_deck import edit_deck
from yt2class.stages import verify_claims as verify_mod
from tests.helpers.m2 import frames_caps, make_transcript, make_visual
from tests.helpers.m3 import concept_unit, course_map, grounding_provider, knowledge
from yt2class.adapters.providers.base import FakeProvider, RequestCancelled
from yt2class.stages.llm_util import model_request


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


def test_provider_runs_distinct_requests_concurrently():
    class SlowProvider(FakeProvider):
        def __init__(self):
            super().__init__(frames_caps())
            self.active = 0
            self.peak = 0
            self.state_lock = threading.Lock()
            self.barrier = threading.Barrier(2)

        def _complete(self, request, *, cancel_event=None):
            with self.state_lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            self.barrier.wait(timeout=1.0)
            try:
                return super()._complete(request, cancel_event=cancel_event)
            finally:
                with self.state_lock:
                    self.active -= 1

    provider = SlowProvider()
    requests = [
        model_request(request_id=f"parallel-{index}", role="verifier", payload={"index": index})
        for index in range(2)
    ]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(provider.complete, requests))

    assert len(results) == 2
    assert provider.peak == 2


def test_same_request_waiter_can_cancel_without_cancelling_owner():
    class BlockingProvider(FakeProvider):
        def __init__(self):
            super().__init__(frames_caps())
            self.started = threading.Event()
            self.release = threading.Event()

        def _complete(self, request, *, cancel_event=None):
            self.started.set()
            assert self.release.wait(timeout=1.0)
            return super()._complete(request, cancel_event=cancel_event)

    class ObservableCancel(threading.Event):
        def __init__(self):
            super().__init__()
            self.checked = threading.Event()

        def is_set(self):
            self.checked.set()
            return super().is_set()

    provider = BlockingProvider()
    request = model_request(request_id="shared-request", role="verifier", payload={"ok": True})
    waiter_cancel = ObservableCancel()
    with ThreadPoolExecutor(max_workers=2) as pool:
        owner = pool.submit(provider.complete, request)
        assert provider.started.wait(timeout=1.0)
        waiter = pool.submit(provider.complete, request, cancel_event=waiter_cancel)
        assert waiter_cancel.checked.wait(timeout=1.0)
        waiter_cancel.set()
        try:
            with pytest.raises(RequestCancelled):
                waiter.result(timeout=0.2)
        finally:
            provider.release.set()
        assert owner.result(timeout=1.0).request_id == "shared-request"


def test_verify_structural_checks_cap_concurrency(monkeypatch):
    units = []
    caps = []
    for index in range(6):
        cap_id = f"cap-{index:03d}"
        t0 = index * 10.0
        units.append(
            concept_unit(
                f"unit-{index:03d}",
                f"claim-{index:03d}",
                f"概念{index}：定义要点。",
                [cap_id],
                start=t0,
                end=t0 + 10.0,
            )
        )
        caps.append((cap_id, t0, t0 + 10.0, f"概念{index}：定义要点。"))
    doc = knowledge(*units)
    topics = course_map([("topic-1", "并发", 0.0, 60.0)])
    transcript = make_transcript(caps, duration=60.0)
    visual = make_visual([], duration=60.0)
    plan = edit_deck(
        doc,
        course_map=topics,
        transcript=transcript,
        visual=visual,
        provider=FakeProvider(frames_caps()),
        target_pages=8,
        max_pages=10,
    )

    active = 0
    peak = 0
    lock = threading.Lock()
    original = verify_mod.check_numbers

    def slow_numbers(claim, evidence_text):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        threading.Event().wait(0.02)
        try:
            return original(claim, evidence_text)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(verify_mod, "check_numbers", slow_numbers)
    verify_mod.verify_claims(
        doc,
        plan=plan,
        transcript=transcript,
        visual=visual,
        provider=grounding_provider(),
        quality_mode="draft",
    )
    assert peak <= MAX_PROVIDER_CONCURRENCY
    assert peak >= 2
