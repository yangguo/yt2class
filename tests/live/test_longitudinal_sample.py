"""Opt-in live longitudinal sample. Skipped unless explicit env paths exist.

This is not a FakeProvider test and does not commit media, secrets, or raw
model gold answers.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live

REQUIRED_ENV = (
    "YT2CLASS_LIVE_SAMPLE",
    "YT2CLASS_LIVE_CAPTIONS",
    "YT2CLASS_LIVE_ANNOTATIONS",
)


def _configured() -> bool:
    return all(os.environ.get(name) for name in REQUIRED_ENV)


@pytest.mark.skipif(not _configured(), reason="live sample paths are not configured")
def test_live_longitudinal_sample_is_explicit_and_local():
    media = Path(os.environ["YT2CLASS_LIVE_SAMPLE"]).expanduser()
    captions = Path(os.environ["YT2CLASS_LIVE_CAPTIONS"]).expanduser()
    annotations = Path(os.environ["YT2CLASS_LIVE_ANNOTATIONS"]).expanduser()
    assert media.is_file(), "live media must be a local licensed file"
    assert captions.is_file(), "live captions must be a local sidecar"
    payload = json.loads(annotations.read_text(encoding="utf-8"))
    assert payload.get("media_path") != "NOT_COMMITTED"
    assert payload.get("topics")
    # The live path is documented only. Wiring a licensed provider is a
    # follow-up; this harness refuses to treat placeholder JSON as gold.
    pytest.skip("licensed provider run is operator-driven; see tests/live/README.md")
