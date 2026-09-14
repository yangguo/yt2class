from __future__ import annotations

from yt2class.orchestration.cache import compute_cache_key


def test_bind_cache_key_changes_with_review_revision():
    tools = {"producer_version": "p", "yt_dlp": None, "ffmpeg": None, "provider": "fake", "prompt": "x"}
    base = compute_cache_key(
        "bind_spec",
        config_digest="cfg",
        input_hashes=["plan", "report"],
        tool_versions=tools,
        review_revision=0,
    )
    bumped = compute_cache_key(
        "bind_spec",
        config_digest="cfg",
        input_hashes=["plan", "report"],
        tool_versions=tools,
        review_revision=1,
    )
    assert base != bumped
