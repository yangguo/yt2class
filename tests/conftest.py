"""Shared pytest helpers. Prototype PPTX tests need the Artifact Tool runtime."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from yt2class import renderer


def artifact_tool_available() -> bool:
    configured = os.getenv("YT2CLASS_ARTIFACT_SETUP")
    if configured:
        return Path(configured).expanduser().is_file()
    return bool(renderer._runtime_setup_candidates())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if artifact_tool_available():
        return
    skip = pytest.mark.skip(reason="Artifact Tool setup helper is unavailable in this environment")
    for item in items:
        if "artifact_tool" in item.keywords:
            item.add_marker(skip)
