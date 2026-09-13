from __future__ import annotations

from threading import Event, Thread
import sys
import time

import pytest

from yt2class.adapters.process import ProcessCancelled, run_process


def test_default_process_runner_terminates_a_running_command_on_cancel():
    cancelled = Event()

    def trigger_cancel() -> None:
        time.sleep(0.15)
        cancelled.set()

    trigger = Thread(target=trigger_cancel)
    trigger.start()
    started = time.monotonic()
    try:
        with pytest.raises(ProcessCancelled):
            run_process(
                [sys.executable, "-c", "import time; time.sleep(10)"],
                timeout_seconds=10.0,
                cancel_event=cancelled,
            )
    finally:
        trigger.join(timeout=2.0)
    assert time.monotonic() - started < 3.0
