"""Shared cancel event for CLI and batch workers."""

from __future__ import annotations

import signal
from threading import Event

_CANCEL_EVENT: Event | None = None


def shared_cancel_event() -> Event:
    global _CANCEL_EVENT
    if _CANCEL_EVENT is None:
        _CANCEL_EVENT = Event()
    return _CANCEL_EVENT


def install_sigint_handler() -> Event:
    event = shared_cancel_event()

    def _handler(signum, frame) -> None:  # noqa: ARG001
        event.set()

    try:
        signal.signal(signal.SIGINT, _handler)
    except ValueError:
        pass
    return event


__all__ = ["install_sigint_handler", "shared_cancel_event"]
