"""Cancellable shell-free subprocess execution for external adapters."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import signal
import subprocess
import time
from threading import Event
from typing import Sequence


class ProcessCancelled(RuntimeError):
    """Raised after a running child process has been terminated."""


class ProcessTimedOut(RuntimeError):
    """Raised after a running child process exceeds its deadline."""


class ProcessUnavailable(RuntimeError):
    """Raised when the requested executable cannot be started."""


class ProcessError(RuntimeError):
    """Raised when a child process cannot be controlled."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _signal_group(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal the child and every process it started in its session/group."""

    pid = process.pid
    if pid is None:
        return
    if os.name == "posix":
        try:
            os.killpg(pid, sig)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
    except ProcessLookupError:
        return


def _terminate(process: subprocess.Popen[str], *, grace_seconds: float) -> None:
    _signal_group(process, signal.SIGTERM)
    try:
        process.wait(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
    try:
        process.communicate(timeout=grace_seconds)
    except subprocess.TimeoutExpired:
        # The process has already been killed; leave final pipe draining to the
        # OS rather than allowing cancellation to block indefinitely.
        pass


def run_process(
    command: Sequence[str | Path],
    *,
    timeout_seconds: float,
    cancel_event: Event | None = None,
    poll_seconds: float = 0.05,
    terminate_grace_seconds: float = 0.5,
) -> ProcessResult:
    """Run a command with bounded timeout and cooperative cancellation.

    Adapters use this only for their default subprocess runner.  Their injected
    runners remain available for deterministic unit tests and failure injection.
    """

    timeout = float(timeout_seconds)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be finite and greater than zero")
    poll = max(0.01, float(poll_seconds))
    grace = max(0.05, float(terminate_grace_seconds))
    argv = [str(item) for item in command]
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("process cancelled before start")
    try:
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            start_new_session=True,
        )
    except FileNotFoundError as error:
        raise ProcessUnavailable(f"executable is unavailable: {argv[0]}") from error
    except OSError as error:
        raise ProcessError(f"cannot start process: {argv[0]}") from error

    deadline = time.monotonic() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            _terminate(process, grace_seconds=grace)
            raise ProcessCancelled(f"process cancelled: {argv[0]}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate(process, grace_seconds=grace)
            raise ProcessTimedOut(f"process timed out: {argv[0]}")
        try:
            stdout, stderr = process.communicate(timeout=min(poll, remaining))
        except subprocess.TimeoutExpired:
            continue
        return ProcessResult(
            returncode=int(process.returncode),
            stdout=stdout or "",
            stderr=stderr or "",
        )


__all__ = [
    "ProcessCancelled",
    "ProcessError",
    "ProcessResult",
    "ProcessTimedOut",
    "ProcessUnavailable",
    "run_process",
]
