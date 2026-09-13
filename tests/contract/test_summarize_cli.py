"""Offline contract tests for the optional summarize CLI adapter."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from yt2class.adapters.summarize_cli import (
    NativeIngestExpectation,
    SummarizeContractError,
    SummarizeFailedError,
    UPSTREAM_DEFAULT_SLIDES_MAX,
    UPSTREAM_LICENSE,
    UPSTREAM_NODE,
    UPSTREAM_PACKAGE,
    UPSTREAM_VERSION,
    build_extract_command,
    build_slides_command,
    compare_to_native,
    parse_summarize_json,
    run_summarize,
    validate_frame_bytes,
    validate_slide_times,
    validate_source_kind,
)

FIXTURES = Path(__file__).resolve().parents[2] / "tests/fixtures/summarize"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_upstream_pin_is_recorded():
    assert UPSTREAM_PACKAGE == "@steipete/summarize"
    assert UPSTREAM_VERSION == "0.21.14"
    assert UPSTREAM_LICENSE == "MIT"
    assert UPSTREAM_NODE.startswith(">=")
    assert UPSTREAM_DEFAULT_SLIDES_MAX == 6


def test_slides_fixture_times_frames_and_source():
    result = parse_summarize_json(load("slides_ok.json"))
    validate_slide_times(result, duration_seconds=60.0)
    validate_frame_bytes(result, root=FIXTURES)
    validate_source_kind(result)
    assert result.slides is not None
    assert result.slides.source_kind == "local"
    assert [slide.timestamp for slide in result.slides.slides] == [12.5, 40.0]


def test_extract_fixture_keeps_caption_origin():
    result = parse_summarize_json(load("extract_ok.json"))
    validate_source_kind(result)
    assert result.extract is not None
    assert result.extract.segments[0].origin == "sidecar"
    native = NativeIngestExpectation.model_validate(load("native_expectation.local.json"))
    assert compare_to_native(result, native) == []


def test_youtube_and_local_sources_are_distinguishable():
    youtube = parse_summarize_json(load("slides_and_extract_ok.json"))
    local = parse_summarize_json(load("slides_ok.json"))
    validate_source_kind(youtube)
    validate_source_kind(local)
    assert youtube.slides.source_kind == "youtube"
    assert local.slides.source_kind == "local"
    assert youtube.extract.segments[0].origin == "auto-caption"


def test_failed_envelope_is_not_success():
    with pytest.raises(SummarizeFailedError, match="unavailable"):
        parse_summarize_json(load("failed.json"))


def test_nonzero_exit_is_not_success_even_if_ok_true():
    with pytest.raises(SummarizeFailedError, match="exited 1"):
        parse_summarize_json(load("slides_ok.json"), exit_code=1)


def test_invalid_times_fail_the_contract():
    with pytest.raises((ValidationError, SummarizeContractError), match="greater than or equal to 0|timestamp"):
        parse_summarize_json(load("ok_bad_times.json"))


def test_parent_image_path_is_rejected():
    payload = load("slides_ok.json")
    payload["slides"]["slides"][0]["imagePath"] = "../secret.png"
    with pytest.raises(SummarizeContractError, match=r"\.\."):
        parse_summarize_json(payload)


def test_missing_frame_bytes_fail_the_contract(tmp_path):
    result = parse_summarize_json(load("slides_ok.json"))
    with pytest.raises(SummarizeContractError, match="readable file"):
        validate_frame_bytes(result, root=tmp_path)


def test_command_vectors_are_argv_arrays():
    slides = build_slides_command("lesson.mp4", Path("out"))
    extract = build_extract_command("https://youtu.be/fixture0001")
    assert slides == ["summarize", "slides", "--json", "-o", "out", "--", "lesson.mp4"]
    assert extract == ["summarize", "--extract", "--json", "--timestamps", "--", "https://youtu.be/fixture0001"]


def test_subprocess_adapter_uses_injected_runner():
    payload = json.dumps(load("slides_ok.json"))

    def runner(command, **kwargs):
        assert command[0] == "summarize"
        assert kwargs["check"] is False
        return SimpleNamespace(returncode=0, stdout=payload, stderr="")

    result = run_summarize(
        build_slides_command("./synthetic-local.mp4", Path("out")),
        runner=runner,
        frame_root=FIXTURES,
        duration_seconds=60.0,
    )
    assert result.ok is True


def test_subprocess_nonzero_never_parses_as_success():
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=2, stdout='{"ok": true, "slides": {}}', stderr="boom")

    with pytest.raises(SummarizeFailedError, match="exited 2"):
        run_summarize(["summarize", "slides", "x", "--json"], runner=runner)


def test_native_comparison_records_slides_max_gap():
    result = parse_summarize_json(load("slides_ok.json"))
    native = NativeIngestExpectation.model_validate(load("native_expectation.many_frames.json"))
    gaps = compare_to_native(result, native)
    assert any("slides-max" in gap for gap in gaps)
    assert any("timestamps" in gap for gap in gaps)


@pytest.mark.live
def test_live_summarize_is_opt_in_and_skipped_by_default():
    pytest.skip(
        "live summarize/YouTube comparison is not part of ordinary contract tests; "
        "see docs/experiments/summarize-adoption.md"
    )
