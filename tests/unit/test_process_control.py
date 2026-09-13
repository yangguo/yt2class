from __future__ import annotations

from pathlib import Path
from threading import Event, Thread
import sys
import time

import pytest

from yt2class.adapters.process import ProcessCancelled, ProcessTimedOut, run_process


def _child_alive(pid: int) -> bool:
    status = Path(f"/proc/{pid}/status")
    try:
        for line in status.read_text(encoding="ascii").splitlines():
            if line.startswith("State:"):
                return not line.split()[1].startswith("Z")
    except OSError:
        return False
    return True


def _spawn_group_script(pid_path: Path) -> str:
    return (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        f"Path({str(pid_path)!r}).write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(30)\n"
    )


def _wait_until_dead(pid: int, *, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _child_alive(pid):
            return True
        time.sleep(0.05)
    return not _child_alive(pid)


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


def test_cancel_terminates_the_entire_process_group(tmp_path: Path):
    pid_path = tmp_path / "child.pid"
    cancelled = Event()

    def trigger_cancel() -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pid_path.exists():
            time.sleep(0.05)
        time.sleep(0.1)
        cancelled.set()

    trigger = Thread(target=trigger_cancel)
    trigger.start()
    try:
        with pytest.raises(ProcessCancelled):
            run_process(
                [sys.executable, "-c", _spawn_group_script(pid_path)],
                timeout_seconds=10.0,
                cancel_event=cancelled,
            )
    finally:
        trigger.join(timeout=2.0)
    child_pid = int(pid_path.read_text(encoding="ascii"))
    assert _wait_until_dead(child_pid)


def test_sigterm_ignoring_descendant_is_killed_after_grace(tmp_path: Path):
    pid_path = tmp_path / "child.pid"
    script = (
        "import signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([\n"
        "    sys.executable, '-c',\n"
        "    'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)',\n"
        "])\n"
        f"Path({str(pid_path)!r}).write_text(str(child.pid), encoding='ascii')\n"
        "time.sleep(30)\n"
    )
    cancelled = Event()

    def trigger_cancel() -> None:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not pid_path.exists():
            time.sleep(0.05)
        time.sleep(0.1)
        cancelled.set()

    trigger = Thread(target=trigger_cancel)
    trigger.start()
    try:
        with pytest.raises(ProcessCancelled):
            run_process(
                [sys.executable, "-c", script],
                timeout_seconds=10.0,
                cancel_event=cancelled,
                terminate_grace_seconds=0.2,
            )
    finally:
        trigger.join(timeout=2.0)
    child_pid = int(pid_path.read_text(encoding="ascii"))
    assert _wait_until_dead(child_pid)


def test_timeout_terminates_the_entire_process_group(tmp_path: Path):
    pid_path = tmp_path / "child.pid"
    with pytest.raises(ProcessTimedOut):
        run_process(
            [sys.executable, "-c", _spawn_group_script(pid_path)],
            timeout_seconds=0.5,
        )
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not pid_path.exists():
        time.sleep(0.05)
    child_pid = int(pid_path.read_text(encoding="ascii"))
    assert _wait_until_dead(child_pid)
