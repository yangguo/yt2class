"""Unit tests for httpx wall-clock deadline helper."""

from __future__ import annotations

import time

import httpx
import pytest

from yt2class.adapters.http_deadline import post_json_with_wall_clock_deadline
from yt2class.orchestration.retry import RetryableError


def test_post_json_wall_clock_deadline_raises_on_slow_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        time.sleep(1.5)
        return httpx.Response(200, json={"ok": True}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(RetryableError, match="wall-clock deadline"):
        post_json_with_wall_clock_deadline(
            client,
            "https://example.test/v1/chat/completions",
            headers={},
            json_body={"model": "test"},
            deadline_seconds=0.2,
            error_label="test request",
        )
    assert time.monotonic() - started < 1.0
